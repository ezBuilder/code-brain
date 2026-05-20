Run the Claude/Codex kit doctor from the installed kit directory.

Use this when the user asks whether the global Claude/Codex setup is actually installed, broken, stale, or blocked by project-local overrides.

Steps:

1. Locate the kit at `/Users/ezbuilder/workspace/code-brain-global-kit` or `~/.local/share/code-brain-global-kit`.
2. Run `./scripts/doctor.sh` from that directory.
3. If the current project path is known, also run `./scripts/doctor.sh --target "$PWD"`.
4. Report only failed checks, or say the global install and current project override are OK.
