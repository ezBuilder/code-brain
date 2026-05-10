# Code Brain Remote Memory: Cloudflare Worker

This package absorbs the useful part of `rahilp/second-brain-cloudflare`: a small
Cloudflare Worker backed by D1, Vectorize, and Workers AI embeddings.

It intentionally changes the upstream defaults:

- `/mcp`, `/capture`, `/recall`, `/list`, and `/forget` require `Authorization: Bearer <token>`.
- CORS is allowlist-based through `ALLOWED_ORIGINS`; `*` is not used.
- Secret-looking input is rejected instead of silently stored.
- Vectorize metadata stores scoped provenance and a redacted summary, not raw note bodies.
- Recall defaults to `global` plus the current `project_id`; cross-project recall is opt-in.

Code Brain's repo-local `.ai/memory` remains the source of truth. This remote
store is only an opt-in global/project memory backend for Claude, Codex, browser,
or mobile capture.

## Deploy

```bash
npm install
npm run db:create
npm run vectors:create
npm run db:migrate:remote
openssl rand -base64 32 | wrangler secret put AUTH_TOKEN
wrangler secret put ALLOWED_ORIGINS
npm run deploy
```

Set these locally before using the Code Brain adapter:

```bash
export AI_REMOTE_MEMORY_URL="https://code-brain-remote-memory.<account>.workers.dev"
export AI_REMOTE_MEMORY_TOKEN="<token>"
```
