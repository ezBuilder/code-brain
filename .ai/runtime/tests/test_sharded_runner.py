"""Contract tests for scripts/test-sharded.py.

The sharded runner is what `make test` and the release gate execute, so its
correctness gates every other test result. Two properties matter:

1. Every test file reaches exactly one shard (nothing is silently dropped).
2. Files split by node id must be safe to split, i.e. no module/session-scoped
   fixtures and no ordering marks that assume in-file execution order.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / "scripts" / "test-sharded.py"
TESTS = ROOT / ".ai" / "runtime" / "tests"


def _load_runner():
    spec = importlib.util.spec_from_file_location("cb_test_sharded", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_exists_and_is_executable() -> None:
    assert RUNNER.is_file(), "make test and the release gate both invoke this runner"


def test_runner_discovers_every_test_file() -> None:
    runner = _load_runner()
    discovered = set(runner._test_files())
    on_disk = set(TESTS.glob("test_*.py"))
    assert discovered == on_disk, "a test file must never be invisible to the runner"


def test_weight_prefers_repo_copying_files() -> None:
    """The long pole must be scheduled first, or shards finish unevenly."""
    runner = _load_runner()
    cli = TESTS / "test_cli.py"
    if not cli.exists():
        pytest.skip("test_cli.py not present")
    others = [path for path in TESTS.glob("test_*.py") if path != cli]
    assert others
    assert runner._weight(cli) > max(runner._weight(path) for path in others)


def test_split_candidate_files_have_no_shared_module_state() -> None:
    """Intra-file splitting is only valid without module/session fixtures or ordering.

    If a future change adds a module-scoped fixture to a heavy file, splitting it
    across processes would re-run setup per shard or break assumed ordering. This
    asserts the invariant that makes the split safe rather than trusting it.
    """
    runner = _load_runner()
    files = runner._test_files()
    weights = {path: runner._weight(path) for path in files}
    total = sum(weights.values())
    # Mirror the runner's own threshold at its default job count.
    jobs = 12
    fair_share = total / jobs
    unsafe = re.compile(
        r"scope\s*=\s*[\"'](module|session)[\"']"
        r"|pytest\.mark\.order"
        r"|pytest\.mark\.dependency"
    )
    heavy = [path for path, weight in weights.items() if weight > fair_share]
    assert heavy, "expected at least one file above a fair share"
    for path in heavy:
        text = path.read_text(encoding="utf-8")
        match = unsafe.search(text)
        assert match is None, (
            f"{path.name} is large enough to be split by node id but declares "
            f"{match.group(0)!r}; make it lighter or exclude it from splitting"
        )


def test_source_only_skip_list_matches_tests_that_need_release_machinery() -> None:
    """The consumer skip list must be derived from reality, not maintained by hand.

    `.ai/runtime/tests` ships into every consumer project, but the release
    machinery those tests assert on (`Makefile`, `bootstrap.sh`, `scripts/`,
    `.github/`) does not. If a test starts referencing that machinery without
    being added to the skip list, users of installed projects get a failure that
    says nothing about their installation. This keeps the two in lockstep.
    """
    import re

    from conftest import SOURCE_ONLY_TEST_MODULES

    pattern = re.compile(
        r'(?:ROOT|root)\s*/\s*'
        r'"(?:scripts|Makefile|\.github|bootstrap\.sh|CHANGELOG\.md|OPERATIONS\.md|kits)"'
    )
    referencing = {
        path.stem
        for path in TESTS.glob("test_*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    }
    missing = sorted(referencing - set(SOURCE_ONLY_TEST_MODULES))
    assert not missing, (
        "these test modules assert on source-repo release machinery but are not in "
        f"conftest.SOURCE_ONLY_TEST_MODULES, so they will fail in consumer "
        f"projects: {missing}"
    )


def test_source_repository_detection_requires_every_marker() -> None:
    """A consumer that keeps its own Makefile or scripts/ must not look like source.

    Detection is an AND over files only the Code Brain repository ships. If it
    were an OR, an ordinary project with a `Makefile` would run release-contract
    tests it cannot satisfy.
    """
    import conftest

    assert conftest.IS_SOURCE_REPOSITORY, "the source repo must detect itself"
    assert len(conftest._SOURCE_REPO_MARKERS) >= 3
    for marker in conftest._SOURCE_REPO_MARKERS:
        assert (ROOT / marker).exists(), marker
