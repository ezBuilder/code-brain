# Contributing to Code Brain

Thanks for helping improve Code Brain. This repo is the public source for a repo-local agent infrastructure kit; changes here ship to every installed project via the upgrade path, so correctness and reproducibility matter.

## Development Setup

Requirements: `git`, `make`, [`uv`](https://docs.astral.sh/uv/) (manages Python 3.11+), and `bash`. PowerShell is optional (Windows shims).

```bash
git clone https://github.com/ezBuilder/code-brain.git
cd code-brain
./bootstrap.sh          # sets up the runtime venv and renders generated files
```

## Branch Model

- Work on `develop` (the default branch). Open PRs against `develop`.
- `main` tracks released state; releases are tagged from it.
- Do not hand-edit generated/owned files (`.ai/generated/manifest.json`, `.ai/cache/**`, lockfiles) — regenerate them.

## Before You Open a PR

```bash
make lint                  # shell + Python + Makefile + PowerShell parsing
make test                  # full suite (or targeted: pytest .ai/runtime/tests/test_<area>.py)
make doctor                # config, index, manifest, audit chain, secret scan, SLOs
.ai/bin/ai index rebuild   # keep the search index fresh if you changed files
```

Guidelines:

- Make the smallest coherent change; preserve validation, security, and accessibility behavior.
- Keep hooks and MCP request handling off the network (a hard invariant — see [SECURITY.md](SECURITY.md)).
- No plaintext secrets in tracked source; the secret scanner gates CI.
- Add or update tests for behavior changes. Version literals in tests must track the runtime version.
- **Update the docs.** Any feature add/change must update `README.md` and keep the translations in `docs/readme/` in sync.

## Releases

Releases follow [RELEASE.md](RELEASE.md): bump the version sources, run `make release-gate` (it must report `release_ready: true` on a clean tree), tag `vX.Y.Z`, push, and publish a GitHub Release.

## License

By contributing, you agree your contributions are licensed under the [Apache-2.0](LICENSE) license.
