SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

LATEST_ARCHIVE := $(shell ls -t dist/code-brain-*.tar.gz 2>/dev/null | head -n 1)

.PHONY: help env-check preflight lockfile-check lock-check session-start install-into upgrade-in uninstall-from lint bootstrap test doctor quick smoke docs-check package verify-artifacts install-check reproducibility-check tamper-check rollback-drill bootstrap-idempotency release-gate report release-notes clean-cache clean-artifacts clean-all

help:
	@printf '%s\n' \
		'Targets:' \
		'  make env-check         Verify required local toolchain' \
		'  make preflight         Verify fresh-clone bootstrap readiness' \
		'  make lockfile-check    Verify uv.lock matches runtime dependencies' \
		'  make session-start     Auto-prepare index, hook, and health for a new agent session' \
		'  make install-into TARGET=/repo  Install Code Brain into an existing repo' \
		'  make upgrade-in TARGET=/repo    Upgrade an existing Code Brain install' \
		'  make uninstall-from TARGET=/repo Remove Code Brain managed files from a repo' \
		'  make lint              Run static script and Python compile checks' \
		'  make quick             Run fast local health checks' \
		'  make package           Build release artifacts under dist/' \
		'  make verify-artifacts  Verify checksum, manifest, SBOM, provenance, release notes' \
		'  make install-check     Verify extracted package execution' \
		'  make reproducibility-check Verify repeated package build produces the same archive' \
		'  make tamper-check      Verify corrupted artifacts are rejected' \
		'  make rollback-drill    Verify upgrade backup rollback in a temporary copy' \
		'  make bootstrap-idempotency Verify repeated bootstrap leaves tracked source stable' \
		'  make release-gate      Run the full release gate' \
		'  make report            Print release status JSON' \
		'  make clean-cache       Remove ignored runtime cache files' \
		'  make clean-artifacts   Remove dist/ release artifacts' \
		'  make clean-all         Remove cache, venv, and dist artifacts'

env-check:
	./scripts/env-check.sh

preflight:
	./scripts/preflight.sh --check-only --json

lockfile-check:
	./scripts/lockfile-check.sh

lock-check: lockfile-check

session-start:
	uv run --project .ai/runtime ai session start --agent operator --json

install-into:
	@test -n "$(TARGET)" || (echo "TARGET=/path/to/repo is required" >&2; exit 2)
	./scripts/install-into.sh install "$(TARGET)"

upgrade-in:
	@test -n "$(TARGET)" || (echo "TARGET=/path/to/repo is required" >&2; exit 2)
	./scripts/install-into.sh upgrade "$(TARGET)"

uninstall-from:
	@test -n "$(TARGET)" || (echo "TARGET=/path/to/repo is required" >&2; exit 2)
	./scripts/install-into.sh uninstall "$(TARGET)"

lint:
	./scripts/lint.sh

bootstrap:
	./bootstrap.sh

test:
	env -u CI -u GITHUB_ACTIONS -u GITLAB_CI -u AI_CI uv run --project .ai/runtime python -m pytest .ai/runtime/tests

doctor:
	uv run --project .ai/runtime ai doctor --strict --json

quick: env-check lint doctor
	uv run --project .ai/runtime ai report status --json >/dev/null

smoke:
	./scripts/smoke.sh

docs-check:
	./scripts/docs-check.sh

package:
	./scripts/package.sh

verify-artifacts:
	@if [[ -z "$(LATEST_ARCHIVE)" ]]; then \
		echo "no release archive found; run make package first" >&2; \
		exit 2; \
	fi
	./scripts/verify-artifacts.sh "$(LATEST_ARCHIVE)"

install-check:
	@if [[ -z "$(LATEST_ARCHIVE)" ]]; then \
		echo "no release archive found; run make package first" >&2; \
		exit 2; \
	fi
	./scripts/install-check.sh "$(LATEST_ARCHIVE)"

reproducibility-check:
	@if [[ -z "$(LATEST_ARCHIVE)" ]]; then \
		echo "no release archive found; run make package first" >&2; \
		exit 2; \
	fi
	./scripts/reproducibility-check.sh "$(LATEST_ARCHIVE)"

tamper-check:
	@if [[ -z "$(LATEST_ARCHIVE)" ]]; then \
		echo "no release archive found; run make package first" >&2; \
		exit 2; \
	fi
	./scripts/artifact-tamper-check.sh "$(LATEST_ARCHIVE)"

rollback-drill:
	./scripts/rollback-drill.sh

bootstrap-idempotency:
	./scripts/bootstrap-idempotency.sh

release-gate:
	./scripts/release-gate.sh

report:
	uv run --project .ai/runtime ai report status --json

release-notes:
	uv run --project .ai/runtime ai report release-notes

clean-cache:
	rm -rf .ai/cache .ai/runtime/.pytest_cache .ai/runtime/src/ai_core/__pycache__ .ai/runtime/src/ai_core/worker/__pycache__ .ai/runtime/tests/__pycache__

clean-artifacts:
	rm -rf dist

clean-all: clean-cache clean-artifacts
	rm -rf .ai/runtime/.venv
