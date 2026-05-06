SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

LATEST_ARCHIVE := $(shell ls -t dist/code-brain-*.tar.gz 2>/dev/null | head -n 1)

.PHONY: help lint bootstrap test doctor quick smoke docs-check package verify-artifacts install-check tamper-check release-gate report release-notes clean-cache

help:
	@printf '%s\n' \
		'Targets:' \
		'  make lint              Run static script and Python compile checks' \
		'  make quick             Run fast local health checks' \
		'  make package           Build release artifacts under dist/' \
		'  make verify-artifacts  Verify checksum, manifest, SBOM, provenance' \
		'  make install-check     Verify extracted package execution' \
		'  make tamper-check      Verify corrupted artifacts are rejected' \
		'  make release-gate      Run the full release gate' \
		'  make report            Print release status JSON'

lint:
	./scripts/lint.sh

bootstrap:
	./bootstrap.sh

test:
	uv run --project .ai/runtime python -m pytest .ai/runtime/tests

doctor:
	uv run --project .ai/runtime ai doctor --strict --json

quick: lint doctor test
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

tamper-check:
	@if [[ -z "$(LATEST_ARCHIVE)" ]]; then \
		echo "no release archive found; run make package first" >&2; \
		exit 2; \
	fi
	./scripts/artifact-tamper-check.sh "$(LATEST_ARCHIVE)"

release-gate:
	./scripts/release-gate.sh

report:
	uv run --project .ai/runtime ai report status --json

release-notes:
	uv run --project .ai/runtime ai report release-notes

clean-cache:
	rm -rf .ai/cache .ai/runtime/.pytest_cache .ai/runtime/src/ai_core/__pycache__ .ai/runtime/src/ai_core/worker/__pycache__ .ai/runtime/tests/__pycache__
