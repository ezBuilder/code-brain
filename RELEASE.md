# Release Checklist

## Required Gate

```bash
./bootstrap.sh
./scripts/smoke.sh
uv run --project .ai/runtime ai doctor --strict --json
git status --short
```

## Expected State

- All tests pass.
- Doctor is green.
- Smoke test completes in a temporary repository copy.
- Runtime artifacts remain ignored under `.ai/cache/`, `.ai/runtime/.venv/`, `.ai/runtime/.pytest_cache/`, and `__pycache__/`.
- `git status --short` is empty.

## Compatibility

- Runtime version follows SemVer.
- Protocol version is `1`.
- `ai upgrade plan --target-version <version>` must be compatible before `ai upgrade apply`.
- CI must reject write commands with exit `16`.
