# Tool use templates

Two flavors:

- **Client tools** — Claude returns a `tool_use` block, your app executes
  the function, you feed back a `tool_result`. Use for app-specific
  actions (write to DB, call your internal API, post a comment).
- **Server tools** — Anthropic-hosted (web search, code execution). You
  do not implement these; just enable them in the request.

Default to client tools for product logic. Use server tools only when
the capability is something Anthropic genuinely runs better than you
(web search at the API edge, sandboxed code execution).

## Strict mode

When the tool input shape matters (it almost always does), set
`strict: true` on the tool definition. The model will refuse to emit
malformed input rather than hallucinating fields.

## Files

- `post_comment.json` — a client-tool definition for posting a comment
  to Navio. Use strict mode; require explicit `dryRun` flag so the model
  cannot accidentally post live.
