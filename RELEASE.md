# Release Checklist

## Required Gate

```bash
./bootstrap.sh
./scripts/smoke.sh
./scripts/package.sh
./scripts/install-check.sh
uv run --project .ai/runtime ai doctor --strict --json
git status --short
```

## Expected State

- All tests pass.
- Doctor is green.
- Smoke test completes in a temporary repository copy.
- Runtime artifacts remain ignored under `.ai/cache/`, `.ai/runtime/.venv/`, `.ai/runtime/.pytest_cache/`, and `__pycache__/`.
- Release archives are written to ignored `dist/` with `.sha256` checksums.
- Install check verifies the tarball in a temporary directory, including `.ai/bin/ai` and `.ai/bin/ai-hook`.
- `git status --short` is empty.

## Compatibility

- Runtime version follows SemVer.
- Protocol version is `1`.
- `ai upgrade plan --target-version <version>` must be compatible before `ai upgrade apply`.
- CI must reject write commands with exit `16`.

## Build Artifact

```bash
./scripts/package.sh
./scripts/install-check.sh
```

The package script emits:

- `dist/code-brain-<version>.tar.gz`
- `dist/code-brain-<version>.tar.gz.sha256`
