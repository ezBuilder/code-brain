import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { z } from "zod";

export interface Env {
  DB: D1Database;
  VECTORIZE: VectorizeIndex;
  AI: Ai;
  AUTH_TOKEN: string;
  ALLOWED_ORIGINS?: string;
}

type EntryInput = {
  content: string;
  tags?: string[];
  scope?: "global" | "project";
  project_id?: string;
  repo_url?: string;
  source_agent?: string;
  source_surface?: string;
  sensitivity?: string;
  dedupe_hash?: string;
  expires_at?: number | null;
};

const SECRET_PATTERNS = [
  /(api[_-]?key|secret|token|password)\s*[:=]\s*['"]?[A-Za-z0-9./+=-]{20,}['"]?/i,
  /AKIA[0-9A-Z]{16}/,
  /-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----/,
  /gh[opsu]_[A-Za-z0-9_]{20,}/
];

function corsHeaders(request: Request, env: Env): HeadersInit {
  const origin = request.headers.get("Origin") || "";
  const allowed = (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const headers: Record<string, string> = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept"
  };
  if (origin && allowed.includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Vary"] = "Origin";
  }
  return headers;
}

function json(request: Request, env: Env, data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders(request, env) }
  });
}

function isAuthorized(request: Request, env: Env): boolean {
  return request.headers.get("Authorization") === `Bearer ${env.AUTH_TOKEN}`;
}

function rejectUnauthorized(request: Request, env: Env): Response | null {
  return isAuthorized(request, env) ? null : json(request, env, { ok: false, error: "unauthorized" }, 401);
}

function assertNoSecret(text: string): void {
  if (SECRET_PATTERNS.some((pattern) => pattern.test(text))) {
    throw new Error("secret_like_content_rejected");
  }
}

function normalizeTags(tags: unknown): string[] {
  if (!Array.isArray(tags)) return [];
  return Array.from(new Set(tags.map((tag) => String(tag).trim().slice(0, 64)).filter(Boolean))).sort();
}

function summarize(text: string): string {
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length > 220 ? `${compact.slice(0, 217)}...` : compact;
}

async function embed(text: string, env: Env): Promise<number[]> {
  const result = (await env.AI.run("@cf/baai/bge-small-en-v1.5" as never, { text: [text] })) as { data: number[][] };
  return result.data[0];
}

async function storeEntry(input: EntryInput, env: Env): Promise<{ ok: true; id: string }> {
  const content = input.content.trim();
  if (!content) throw new Error("content_required");
  assertNoSecret(content);

  const now = Date.now();
  const id = crypto.randomUUID();
  const tags = normalizeTags(input.tags);
  const scope = input.scope === "global" ? "global" : "project";
  const projectId = String(input.project_id || "").trim() || "unknown";
  const summary = summarize(content);
  const dedupeHash = input.dedupe_hash || await sha256(content);

  await env.DB.prepare(
    `INSERT INTO entries
      (id, content_redacted, summary, tags, scope, project_id, repo_url, source_agent,
       source_surface, sensitivity, dedupe_hash, created_at, updated_at, expires_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).bind(
    id,
    content,
    summary,
    JSON.stringify(tags),
    scope,
    projectId,
    String(input.repo_url || "").slice(0, 300),
    String(input.source_agent || "unknown").slice(0, 64),
    String(input.source_surface || "api").slice(0, 64),
    String(input.sensitivity || "internal").slice(0, 32),
    dedupeHash,
    now,
    now,
    input.expires_at || null
  ).run();

  const values = await embed(content, env);
  await env.VECTORIZE.insert([{
    id,
    values,
    metadata: {
      summary,
      scope,
      project_id: projectId,
      tags,
      source_agent: String(input.source_agent || "unknown").slice(0, 64),
      source_surface: String(input.source_surface || "api").slice(0, 64),
      created_at: now
    }
  }]);

  return { ok: true, id };
}

async function recallEntries(args: {
  query: string;
  topK?: number;
  project_id?: string;
  include_cross_project?: boolean;
  scope?: "global" | "project";
}, env: Env): Promise<{ ok: true; matches: unknown[] }> {
  const query = args.query.trim();
  if (!query) throw new Error("query_required");
  assertNoSecret(query);
  const projectId = String(args.project_id || "").trim() || "unknown";
  const includeCrossProject = Boolean(args.include_cross_project);
  const topK = Math.max(1, Math.min(20, Number(args.topK || 5)));
  const values = await embed(query, env);
  const raw = await env.VECTORIZE.query(values, {
    topK: includeCrossProject ? topK : Math.min(100, topK * 8),
    returnMetadata: "all"
  });
  const matches = raw.matches
    .filter((match) => {
      const meta = match.metadata as Record<string, unknown>;
      if (args.scope && meta.scope !== args.scope) return false;
      if (includeCrossProject) return true;
      return meta.scope === "global" || meta.project_id === projectId;
    })
    .slice(0, topK)
    .map((match) => {
      const meta = match.metadata as Record<string, unknown>;
      return {
        id: match.id,
        score: match.score,
        summary: meta.summary,
        scope: meta.scope,
        project_id: meta.project_id,
        source_agent: meta.source_agent,
        source_surface: meta.source_surface,
        created_at: meta.created_at
      };
    });
  return { ok: true, matches };
}

async function listEntriesData(env: Env, args: {
  n?: number;
  project_id?: string;
  include_cross_project?: boolean;
}): Promise<{ ok: true; entries: unknown[] }> {
  const n = Math.max(1, Math.min(50, Number(args.n || 10)));
  const projectId = args.project_id || "unknown";
  const includeCrossProject = Boolean(args.include_cross_project);
  const where = includeCrossProject ? "" : "WHERE scope = 'global' OR project_id = ?";
  const stmt = env.DB.prepare(
    `SELECT id, summary, tags, scope, project_id, source_agent, source_surface, created_at
     FROM entries ${where} ORDER BY created_at DESC LIMIT ?`
  );
  const bound = includeCrossProject ? stmt.bind(n) : stmt.bind(projectId, n);
  const result = await bound.all();
  return { ok: true, entries: result.results || [] };
}

async function listEntries(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  return json(request, env, await listEntriesData(env, {
    n: Number(url.searchParams.get("n") || 10),
    project_id: url.searchParams.get("project_id") || "unknown",
    include_cross_project: url.searchParams.get("include_cross_project") === "1"
  }));
}

async function sha256(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function buildMcpServer(env: Env): McpServer {
  const server = new McpServer({ name: "code-brain-remote-memory", version: "0.1.0" });
  server.tool("remember", "Store an explicit scoped remote memory", {
    content: z.string(),
    tags: z.array(z.string()).optional(),
    scope: z.enum(["global", "project"]).default("project"),
    project_id: z.string().default("unknown"),
    source_agent: z.string().default("claude"),
    source_surface: z.string().default("mcp")
  }, async (args) => ({ content: [{ type: "text", text: JSON.stringify(await storeEntry(args, env)) }] }));
  server.tool("recall", "Recall scoped remote memory", {
    query: z.string(),
    topK: z.number().int().min(1).max(20).default(5),
    project_id: z.string().default("unknown"),
    include_cross_project: z.boolean().default(false),
    scope: z.enum(["global", "project"]).optional()
  }, async (args) => ({ content: [{ type: "text", text: JSON.stringify(await recallEntries(args, env)) }] }));
  server.tool("list_recent", "List recent scoped remote memory", {
    n: z.number().int().min(1).max(50).default(10),
    project_id: z.string().default("unknown"),
    include_cross_project: z.boolean().default(false)
  }, async (args) => ({ content: [{ type: "text", text: JSON.stringify(await listEntriesData(env, args)) }] }));
  server.tool("forget", "Delete a remote memory by id", { id: z.string() }, async ({ id }) => {
    await env.DB.prepare("DELETE FROM entries WHERE id = ?").bind(id).run();
    await env.VECTORIZE.deleteByIds([id]);
    return { content: [{ type: "text", text: JSON.stringify({ ok: true, id }) }] };
  });
  return server;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(request, env) });
    }
    const unauthorized = rejectUnauthorized(request, env);
    if (unauthorized) return unauthorized;

    const url = new URL(request.url);
    try {
      if (url.pathname === "/capture" && request.method === "POST") {
        const body = await request.json() as EntryInput;
        return json(request, env, await storeEntry(body, env));
      }
      if (url.pathname === "/recall" && request.method === "POST") {
        const body = await request.json() as Parameters<typeof recallEntries>[0];
        return json(request, env, await recallEntries(body, env));
      }
      if (url.pathname === "/list" && request.method === "GET") {
        return listEntries(request, env);
      }
      if (url.pathname === "/forget" && request.method === "POST") {
        const body = await request.json() as { id?: string };
        const id = String(body.id || "").trim();
        if (!id) return json(request, env, { ok: false, error: "id_required" }, 400);
        await env.DB.prepare("DELETE FROM entries WHERE id = ?").bind(id).run();
        await env.VECTORIZE.deleteByIds([id]);
        return json(request, env, { ok: true, id });
      }
      if (url.pathname === "/mcp") {
        const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined });
        const server = buildMcpServer(env);
        await server.connect(transport);
        const response = await transport.handleRequest(request);
        ctx.waitUntil(response.clone().text().finally(() => server.close()));
        return response;
      }
      return json(request, env, { ok: false, error: "not_found" }, 404);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const status = message.includes("required") || message.includes("rejected") ? 400 : 500;
      return json(request, env, { ok: false, error: message }, status);
    }
  }
};
