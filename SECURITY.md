# Security Policy

Code Brain runs inside developer workspaces and handles repository content, agent memory, and hook execution. We take security seriously.

## Reporting a Vulnerability

**Please do not open public issues for security vulnerabilities.**

Report privately via GitHub's **[Report a vulnerability](https://github.com/ezBuilder/code-brain/security/advisories/new)** (Security tab → Advisories). Include:

- affected version (`ai version`) and platform,
- a clear description and impact,
- reproduction steps or a proof of concept.

You can expect an initial acknowledgement within a few days. Coordinated disclosure is appreciated once a fix is available.

## Supported Versions

Security fixes target the latest released minor version. Pin a known-good release with `ai upgrade latest --ref vX.Y.Z`.

## Design Invariants

Code Brain is built so the common failure modes are closed by default:

- **No network on hot paths.** `SessionStart`/`UserPromptSubmit` hooks and MCP request handling never call the network.
- **No secrets in tracked source.** A secret scanner runs in `doctor --strict` and CI; `.env`, keys, tokens, and certs are never read, printed, or committed.
- **No private memory propagation.** Installers never copy source `.ai/memory/*` or runtime state into target projects.
- **CI is read-only.** Write commands are rejected in CI (exit `16`, `CI_READ_ONLY`).
- **Tamper-evident audit.** The audit chain is hash-linked; tampering is reported as `prev_sha_mismatch`.
- **Bounded output.** Hooks block destructive git, broad `grep`/`find` dumps, and oversized output before they leak data or waste tokens.

These are enforced by `make release-gate` and the checks in [RELEASE.md](RELEASE.md).
