SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

.PHONY: help validate doctor codex-doctor dev-loop-once dev-loop-forever harness-once harness-install-once harness-forever evolution-capture evolution-score evolution-promote evolution-snapshot install-claude install-codex install-all dry-run code-brain-health clean-local

help:
	@printf '%s\n' \
		'Targets:' \
		'  make validate          Validate rules, docs, hooks, and settings' \
		'  make doctor            Check installed global assets and local kit health' \
		'  make codex-doctor      Check Codex config.toml and hooks.json' \
		'  make dev-loop-once     Run one research/evaluate/verify development loop' \
		'  make dev-loop-forever  Run continuous research/evaluate/verify loop; intended for tmux' \
		'  make harness-once      Run one autonomous readiness loop without installing' \
		'  make harness-install-once Run one autonomous readiness loop and install' \
		'  make harness-forever   Run continuous readiness loop; intended for tmux' \
		'  make evolution-capture Capture evolution candidates if script exists' \
		'  make evolution-score   Score evolution candidates if script exists' \
		'  make evolution-promote Dry-run promotion if script exists' \
		'  make evolution-snapshot Snapshot evolution state if script exists' \
		'  make dry-run           Show global install actions' \
		'  make install-claude    Install Claude rule, settings, hooks, agents, and skills globally' \
		'  make install-codex     Install rules/AGENTS.md globally' \
		'  make install-all       Install Claude assets and Codex global rule' \
		'  make code-brain-health Run Code Brain health if this repo has .ai/bin/ai' \
		'  make clean-local       Remove local Finder artifacts'

validate:
	./scripts/validate.sh

doctor:
	./scripts/doctor.sh

codex-doctor:
	./scripts/codex-doctor.sh

dev-loop-once:
	./scripts/dev-loop.sh --once

dev-loop-forever:
	./scripts/dev-loop.sh --forever --sleep 3600

harness-once:
	./scripts/harness.sh --once

harness-install-once:
	./scripts/harness.sh --once --install

harness-forever:
	./scripts/harness.sh --forever --install

evolution-capture:
	./scripts/evolve-capture.sh --source make --candidate "manual evolution capture" --signal "requested" --note "manual target invocation" --tag make

evolution-score:
	./scripts/evolve-score.py

evolution-promote:
	./scripts/evolve-promote.sh --dry-run

evolution-snapshot:
	./scripts/evolve-snapshot.sh snapshot

dry-run:
	./install.sh --all --dry-run

install-claude:
	./install.sh --claude --yes

install-codex:
	./install.sh --codex --yes

install-all:
	./install.sh --all --yes

code-brain-health:
	@if [[ -x .ai/bin/ai ]]; then \
		.ai/bin/ai session start --agent codex --json; \
		.ai/bin/ai obs health-summary --json; \
	else \
		echo 'Code Brain is not installed in this repo'; \
	fi

clean-local:
	find . -name .DS_Store -delete
