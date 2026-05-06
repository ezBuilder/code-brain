# Code Brain Agent Contract

This repository uses `.ai/` as the single repo-local source for AI agent context, memory, generated metadata, trust, and runtime tooling.

## Hard Constraints

- The worker is the only source-of-truth writer. All persistent writes must go through worker IPC after M2.
- No hook or MCP hot path may call the network.
- Embeddings, remote LLM calls, reranking, and external notification channels are off by default.
- CI is read-only. Write commands are rejected at parse time before worker contact.
- Tracked source may not contain plaintext secrets.
- Only SOPS+age ciphertext may be tracked under `.ai/secrets/*.enc.yaml`.
- `.ai/cache/code.sqlite` is the single cache database.
- `.ai/generated/manifest.json` is the single metadata owner.
- `--no-redact` may affect only local stdout with interactive TTY, `--yes`, and audit.
- MCP, external channels, and diagnostics are always redacted.

## MVP Status

This scaffold implements M0-M1 foundations:

- repo layout
- uv runtime
- `ai` CLI
- `ai render`
- `ai doctor`
- CI read-only command rejection
- manifest generation
- basic secret scanning and policy checks

