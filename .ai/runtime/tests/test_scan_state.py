from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ai_core import scan_state


def _file(tmp_path: Path, text: str = "safe\n") -> Path:
    path = tmp_path / "src" / "main.py"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_incremental_scan_reuses_unchanged_file_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    calls = {"count": 0}

    def first_matcher(text: str) -> bool:
        calls["count"] += 1
        return False

    monkeypatch.setattr(scan_state, "contains_secret", first_matcher)
    assert scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=False,
        update_state=True,
    ) == []

    report = scan_state.scan_paths_report(
        tmp_path,
        [path],
        incremental=True,
        update_state=False,
    )
    assert report == {
        "hits": [],
        "mode": "incremental",
        "total": 1,
        "reused": 1,
        "rescanned": 0,
        "unreadable": 0,
        "unstable": 0,
    }
    assert calls["count"] == 1

    def unexpected_matcher(_text: str) -> bool:
        raise AssertionError("unchanged stat-bound result must be reused")

    monkeypatch.setattr(scan_state, "contains_secret", unexpected_matcher)
    assert scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=True,
        update_state=False,
    ) == []


def test_regular_file_state_uses_single_lstat(
    tmp_path: Path,
) -> None:
    path = _file(tmp_path)

    class RegularPathProbe:
        def lstat(self):
            return path.lstat()

        def stat(self):
            raise AssertionError("regular files must not need a second target stat")

        def resolve(self):
            raise AssertionError("regular files must not resolve their own path")

    state = scan_state._path_state(RegularPathProbe())  # type: ignore[arg-type]

    assert state is not None
    assert state["kind"] == "regular"
    assert state["link_target"] is None


def test_incremental_scan_detects_same_size_change_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path, "AAAA\n")
    monkeypatch.setattr(scan_state, "contains_secret", lambda text: text.startswith("B"))
    assert scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=False,
        update_state=True,
    ) == []
    original = path.stat()
    path.write_text("BBBB\n", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=True,
        update_state=True,
    ) == ["src/main.py"]


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_symlink_scan_reads_link_text_not_external_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external.txt"
    external.write_text("token=" + "x" * 24 + "\n", encoding="utf-8")
    linked = tmp_path / "src" / "external-link"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)

    hits = scan_state.scan_paths(
        tmp_path,
        [linked],
        incremental=False,
        update_state=False,
    )

    assert hits == []


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_broken_symlink_target_text_is_scanned(
    tmp_path: Path,
) -> None:
    linked = tmp_path / "src" / "credential-link"
    linked.parent.mkdir(parents=True)
    linked.symlink_to("token=" + "y" * 24)

    hits = scan_state.scan_paths(
        tmp_path,
        [linked],
        incremental=False,
        update_state=False,
    )

    assert hits == ["src/credential-link"]


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_external_target_change_does_not_invalidate_symlink_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external.txt"
    external.write_text("first\n", encoding="utf-8")
    linked = tmp_path / "src" / "external-link"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)
    assert scan_state.scan_paths(
        tmp_path,
        [linked],
        incremental=False,
        update_state=True,
    ) == []
    external.write_text("token=" + "z" * 24 + "\n", encoding="utf-8")

    def unexpected_matcher(_text: str) -> bool:
        raise AssertionError("unchanged symlink payload must reuse cached result")

    monkeypatch.setattr(scan_state, "contains_secret", unexpected_matcher)
    assert scan_state.scan_paths(
        tmp_path,
        [linked],
        incremental=True,
        update_state=False,
    ) == []


def test_full_scan_ignores_cached_negative_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)
    scan_state.scan_paths(tmp_path, [path], incremental=False, update_state=True)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: True)

    hits = scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=False,
        update_state=False,
    )

    assert hits == ["src/main.py"]


def test_invalid_matcher_fingerprint_forces_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)
    scan_state.scan_paths(tmp_path, [path], incremental=False, update_state=True)
    cache = tmp_path / ".ai" / "cache" / "scan-state.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["matcher_fingerprint"] = "stale"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: True)

    hits = scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=True,
        update_state=False,
    )

    assert hits == ["src/main.py"]


def test_matcher_fingerprint_changes_with_implementation_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = scan_state._matcher_fingerprint()
    monkeypatch.setattr(scan_state, "_matcher_implementation_digest", lambda: "0" * 64)
    second = scan_state._matcher_fingerprint()

    assert len(first) == 64
    assert len(second) == 64
    assert second != first


def test_read_only_scan_does_not_write_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)

    scan_state.scan_paths(tmp_path, [path], incremental=False, update_state=False)

    assert not (tmp_path / ".ai" / "cache" / "scan-state.json").exists()


def test_scan_state_file_is_private_on_unix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)
    scan_state.scan_paths(tmp_path, [path], incremental=False, update_state=True)
    cache = tmp_path / ".ai" / "cache" / "scan-state.json"

    assert cache.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink trust boundary")
def test_symlinked_scan_state_is_ignored_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    cache = tmp_path / ".ai" / "cache" / "scan-state.json"
    cache.parent.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_text('{"poisoned": true}\n', encoding="utf-8")
    cache.symlink_to(external)
    calls = {"count": 0}

    def matcher(_text: str) -> bool:
        calls["count"] += 1
        return False

    monkeypatch.setattr(scan_state, "contains_secret", matcher)
    scan_state.scan_paths(tmp_path, [path], incremental=True, update_state=True)

    assert calls["count"] == 1
    assert external.read_text(encoding="utf-8") == '{"poisoned": true}\n'
    assert cache.is_file() and not cache.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink trust boundary")
def test_external_cache_parent_disables_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    external = tmp_path.with_name(tmp_path.name + "-sibling-cache")
    external.mkdir()
    ai = tmp_path / ".ai"
    ai.mkdir(exist_ok=True)
    (ai / "cache").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)

    scan_state.scan_paths(tmp_path, [path], incremental=False, update_state=True)

    assert not (external / "scan-state.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix mode trust boundary")
def test_public_scan_state_mode_forces_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)
    scan_state.scan_paths(tmp_path, [path], incremental=False, update_state=True)
    cache = tmp_path / ".ai" / "cache" / "scan-state.json"
    cache.chmod(0o644)
    calls = {"count": 0}

    def matcher(_text: str) -> bool:
        calls["count"] += 1
        return False

    monkeypatch.setattr(scan_state, "contains_secret", matcher)
    scan_state.scan_paths(tmp_path, [path], incremental=True, update_state=False)

    assert calls["count"] == 1


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hardlinked_scan_state_forces_rescan_without_modifying_external(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)
    scan_state.scan_paths(tmp_path, [path], incremental=False, update_state=True)
    cache = tmp_path / ".ai" / "cache" / "scan-state.json"
    content = cache.read_text(encoding="utf-8")
    cache.unlink()
    external = tmp_path / "external-scan-state.json"
    external.write_text(content, encoding="utf-8")
    if os.name != "nt":
        external.chmod(0o600)
    os.link(external, cache)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: True)

    hits = scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=True,
        update_state=False,
    )

    assert hits == ["src/main.py"]
    assert external.read_text(encoding="utf-8") == content


def test_unstable_file_is_conservatively_flagged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _file(tmp_path)
    real_state = scan_state._path_state(path)
    assert real_state is not None
    counter = {"value": 0}

    def changing_state(_path: Path):
        counter["value"] += 1
        state = dict(real_state)
        state["target"] = list(real_state["target"])
        state["target"][-1] = int(state["target"][-1]) + counter["value"]
        return state

    monkeypatch.setattr(scan_state, "_path_state", changing_state)
    monkeypatch.setattr(scan_state, "contains_secret", lambda _text: False)

    hits = scan_state.scan_paths(
        tmp_path,
        [path],
        incremental=False,
        update_state=False,
    )

    assert hits == ["src/main.py"]
