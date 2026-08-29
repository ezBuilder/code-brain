from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def remove_runtime_test_tmp_path(tmp_path: Path):
    """Delete each test's temporary repository before the next test starts."""
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def remove_runtime_test_basetemp(tmp_path_factory: pytest.TempPathFactory):
    """Runtime tests own their basetemp and must not retain repository copies."""
    basetemp = tmp_path_factory.getbasetemp()
    yield
    shutil.rmtree(basetemp, ignore_errors=True)


# --- Source-repository-only tests -------------------------------------------
#
# The installer intentionally ships `.ai/runtime/tests` into every consumer
# project but not the source repository's own release machinery (`Makefile`,
# `bootstrap.sh`, `scripts/`, `.github/`, `CHANGELOG.md`). Tests that assert on
# that machinery therefore cannot pass in a consumer checkout: they used to fail
# with FileNotFoundError, so a user who ran the installed suite saw ~18 failures
# that say nothing about their installation.
#
# Detect the source repository by the release files a consumer never receives and
# skip those tests elsewhere. Skipping is correct rather than lenient: the
# assertions are about this repository's release contract, and they still run
# here, where the release gate enforces them.

SOURCE_ONLY_TEST_MODULES = frozenset(
    {
        "test_cli",
        "test_codex_hook_auto_trust",
        "test_docs_contract",
        "test_global_kit_health",
        "test_mcp_config_and_antigravity",
        "test_new_hook_events_p1",
        "test_operations_runtime_contract",
        "test_release_retention",
        "test_repo_evals",
        "test_sharded_runner",
        "test_storage_lifecycle",
        "test_storage_referenced_fixture_protection",
    }
)

# All of these exist only in the Code Brain source repository. Requiring every
# one prevents a consumer that happens to keep its own `Makefile` or `scripts/`
# from being misread as the source repo.
_SOURCE_REPO_MARKERS = (
    "Makefile",
    "bootstrap.sh",
    "CHANGELOG.md",
    "scripts/release-gate.sh",
    "scripts/package.sh",
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _is_source_repository() -> bool:
    return all((REPO_ROOT / marker).exists() for marker in _SOURCE_REPO_MARKERS)


IS_SOURCE_REPOSITORY = _is_source_repository()


# `test_codex_hook_auto_trust` imports `scripts.codex_hook_contract` at module
# scope, so in a consumer it fails during collection, before any skip marker can
# apply. Ignoring the file outright is the only way to keep a consumer run clean.
collect_ignore = [] if IS_SOURCE_REPOSITORY else [f"{name}.py" for name in SOURCE_ONLY_TEST_MODULES]


def pytest_collection_modifyitems(config, items):
    """Skip source-repo release-contract tests when running inside a consumer.

    `collect_ignore` already keeps these modules out of a normal consumer run.
    This is the second rail: it still applies when a module is selected
    explicitly by path, which bypasses `collect_ignore`.
    """
    if IS_SOURCE_REPOSITORY:
        return
    reason = (
        "source-repository test: asserts on Code Brain release machinery "
        "(Makefile/bootstrap.sh/scripts/.github) that the installer does not "
        "ship into consumer projects"
    )
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        module = item.nodeid.rsplit("/", 1)[-1].split("::", 1)[0]
        if module.endswith(".py"):
            module = module[: -len(".py")]
        if module in SOURCE_ONLY_TEST_MODULES:
            item.add_marker(skip)
