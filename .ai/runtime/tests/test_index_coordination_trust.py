from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from ai_core import search as search_mod


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ai").mkdir()
    (root / ".ai" / "config.yaml").write_text(
        "project_name: coordination\n",
        encoding="utf-8",
    )
    source = root / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    return root


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_single_flight_rebuild_rejects_linked_lock_without_external_mutation(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = _make_repo(tmp_path)
    lock = root / ".ai" / "cache" / ".rebuild.lock"
    lock.parent.mkdir(parents=True)
    external = tmp_path / f"external-{link_kind}.lock"
    external.write_bytes(b"external-lock")
    external.chmod(0o600)
    original_mode = stat.S_IMODE(external.stat().st_mode)
    if link_kind == "symlink":
        lock.symlink_to(external)
    else:
        os.link(external, lock)

    result = search_mod.rebuild(root, single_flight=True)

    assert result["ok"] is False
    assert result["reason"] == "rebuild_lock_unavailable"
    assert external.read_bytes() == b"external-lock"
    assert stat.S_IMODE(external.stat().st_mode) == original_mode


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_generation_marker_replaces_link_without_external_mutation(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = _make_repo(tmp_path)
    marker = root / ".ai" / "cache" / "code-index-generation"
    marker.parent.mkdir(parents=True)
    external = tmp_path / f"external-generation-{link_kind}.txt"
    external.write_text("external-generation\n", encoding="utf-8")
    external.chmod(0o600)
    if link_kind == "symlink":
        marker.symlink_to(external)
    else:
        os.link(external, marker)

    result = search_mod.rebuild(root)

    assert result["ok"] is True
    assert external.read_text(encoding="utf-8") == "external-generation\n"
    assert not marker.is_symlink()
    assert marker.read_text(encoding="utf-8").strip().isdigit()
    assert marker.stat().st_nlink == 1
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_single_flight_busy_lock_skips_without_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_repo(tmp_path)
    entered: list[Path] = []

    @contextmanager
    def busy_lock(path: Path, *, root: Path):
        entered.append(path)
        yield False

    monkeypatch.setattr(search_mod, "private_file_try_lock", busy_lock)
    monkeypatch.setattr(
        search_mod,
        "_rebuild_inner",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("busy single-flight lock must skip rebuild")
        ),
    )

    result = search_mod.rebuild(root, single_flight=True)

    assert result["ok"] is True
    assert result["skipped"] == "another rebuild in progress"
    assert entered == [root / ".ai" / "cache" / ".rebuild.lock"]
