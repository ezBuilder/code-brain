#!/usr/bin/env python3
"""Run the Code Brain test suite as parallel pytest shards.

Why this exists: the suite is process- and I/O-bound, not CPU-bound in Python.
`test_cli.py` alone copies the repository 138 times and spawns the CLI as a
subprocess per case, so a serial run left a 16-core machine at ~84% of ONE core
and took about 7m40s. Splitting by test file across worker processes cuts that
to roughly the slowest single file.

pytest-xdist would be the obvious tool, but `.ai/runtime` deliberately keeps an
offline certifi+pytest-only lock that the release gate verifies, so this uses
only the standard library and the pytest already in the lock.

Shards are split by FILE, never inside a file, so module-level fixtures and any
file-local ordering stay intact. Exit code is non-zero if any shard fails.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / ".ai" / "runtime" / "tests"

# Tests are run with the CI-ish variables stripped, exactly like `make test`,
# because the runtime intentionally refuses writes when it believes it is in CI.
STRIP_ENV = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "AI_CI")


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in STRIP_ENV:
        env.pop(name, None)
    # Each shard gets its own pytest cache dir via -p no:cacheprovider, so
    # concurrent shards cannot race on .pytest_cache.
    return env


def _test_files() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def _weight(path: Path) -> int:
    """Approximate cost so the long pole starts first.

    Repo-copying subprocess tests dominate wall time, so count those markers
    rather than raw file size.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    return 1 + 50 * text.count("copy_repo(tmp_path)") + text.count("def test_")


def _node_ids(path: Path) -> list[str]:
    """Collect test node ids for one file so a huge file can be split further.

    Collection is cheap (about 1s for the whole suite), and splitting by node id
    is only used for files with no module/session fixtures and no ordering marks.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--co", "-q", str(path)],
        cwd=str(ROOT),
        env=_child_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    # pytest prints node ids relative to ITS rootdir (.ai/runtime), while shards
    # run from the repository root, so rebuild each id against the real file path.
    ids: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" not in line or line.startswith("="):
            continue
        _file_part, _, selector = line.partition("::")
        if selector:
            ids.append(f"{path}::{selector}")
    return ids


def _run_shard(index: int, targets: list[str], verbose: bool) -> tuple[int, int, str]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *targets,
    ]
    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=_child_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.monotonic() - started
    tail = proc.stdout.strip().splitlines()
    summary = tail[-1] if tail else "(no output)"
    label = f"shard {index}: {summary} [{elapsed:.0f}s, {len(targets)} targets]"
    if proc.returncode != 0 or verbose:
        label += "\n" + proc.stdout
    return proc.returncode, index, label


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(8, os.cpu_count() or 4),
        help="parallel shards (default: min(8, cpu_count))",
    )
    parser.add_argument("--verbose", action="store_true", help="always print full shard output")
    args = parser.parse_args()

    files = _test_files()
    if not files:
        print("no test files found", file=sys.stderr)
        return 2

    jobs = max(1, args.jobs)

    # One dominant file (test_cli.py: 181 repo-copying subprocess cases) takes
    # ~90% of wall time, so packing whole files plateaus at that file's runtime.
    # Split any file heavy enough to outweigh a fair share into node ids. This is
    # only safe because such files carry no module/session fixtures and no
    # ordering marks; that invariant is asserted by
    # test_sharded_runner_split_targets_are_independent.
    weights = {path: _weight(path) for path in files}
    total = sum(weights.values())
    fair_share = total / jobs if jobs else total

    targets: list[tuple[str, int]] = []
    for path in files:
        weight = weights[path]
        if weight > fair_share and jobs > 1:
            ids = _node_ids(path)
            if len(ids) > 1:
                per = max(1, weight // len(ids))
                targets.extend((node_id, per) for node_id in ids)
                continue
        targets.append((str(path), weight))

    # Greedy longest-first bin packing.
    shards: list[list[str]] = [[] for _ in range(jobs)]
    loads = [0] * jobs
    for target, weight in sorted(targets, key=lambda item: item[1], reverse=True):
        slot = loads.index(min(loads))
        shards[slot].append(target)
        loads[slot] += weight
    shards = [shard for shard in shards if shard]

    started = time.monotonic()
    failures = 0
    with ThreadPoolExecutor(max_workers=len(shards)) as pool:
        futures = [
            pool.submit(_run_shard, index, shard, args.verbose)
            for index, shard in enumerate(shards)
        ]
        for future in futures:
            code, _index, label = future.result()
            print(label, flush=True)
            if code != 0:
                failures += 1

    total = time.monotonic() - started
    print(f"{len(shards)} shards in {total:.0f}s; {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
