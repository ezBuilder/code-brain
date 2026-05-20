Run one autonomous kit upgrade iteration.

Required loop:

1. Research current official Claude Code and Codex CLI capability surfaces.
2. Compare the findings with `docs/AI_RESEARCH.md`, `docs/AI_DEV_LOOP.md`, installer behavior, and installed assets.
3. Score candidates by impact, safety, implementation size, and verification strength.
4. Implement adopted candidates directly.
5. Run `make validate`, `make doctor`, and `./scripts/dev-loop.sh --once`.
6. Report changed files, adopted/rejected candidates, and verification results.
