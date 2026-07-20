from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from ai_core import search as search_mod


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ai").mkdir()
    (root / ".ai" / "config.yaml").write_text(
        "project_name: index-storage\n",
        encoding="utf-8",
    )
    return root


def _create_external_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("create table sentinel(value text)")
    conn.execute("insert into sentinel(value) values ('external-preserved')")
    conn.commit()
    conn.close()


def _read_sentinel(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("select value from sentinel").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix parent symlink semantics")
def test_index_connect_rejects_external_cache_parent_without_mutation(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    external = tmp_path / "external-cache"
    external.mkdir()
    (root / ".ai" / "cache").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        search_mod.connect(root)

    assert list(external.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_index_connect_replaces_linked_database_without_external_mutation(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = _make_repo(tmp_path)
    db = search_mod.db_path(root)
    db.parent.mkdir(parents=True)
    external = tmp_path / f"external-{link_kind}.sqlite"
    _create_external_db(external)
    external.chmod(0o600)
    if link_kind == "symlink":
        db.symlink_to(external)
    else:
        os.link(external, db)

    conn = search_mod.connect(root)
    search_mod.init_schema(conn)
    conn.close()

    assert _read_sentinel(external) == "external-preserved"
    assert db.stat().st_ino != external.stat().st_ino
    assert not db.is_symlink()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("suffix", search_mod._SQLITE_SIDECAR_SUFFIXES)
def test_index_connect_replaces_linked_sidecar_without_external_mutation(
    tmp_path: Path,
    link_kind: str,
    suffix: str,
) -> None:
    root = _make_repo(tmp_path)
    db = search_mod.db_path(root)
    db.parent.mkdir(parents=True)
    external = tmp_path / f"external-{link_kind}-{suffix.lstrip('-')}.bin"
    external.write_bytes(b"external-sidecar")
    external.chmod(0o600)
    sidecar = Path(str(db) + suffix)
    if link_kind == "symlink":
        sidecar.symlink_to(external)
    else:
        os.link(external, sidecar)

    conn = search_mod.connect(root)
    search_mod.init_schema(conn)
    conn.execute("insert into chunks(path, sha256, summary) values ('a', 'b', 'c')")
    conn.commit()

    assert external.read_bytes() == b"external-sidecar"
    if sidecar.exists():
        assert not sidecar.is_symlink()
        assert sidecar.stat().st_ino != external.stat().st_ino
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    conn.close()


def test_index_connect_preserves_existing_database_and_repairs_mode(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    db = search_mod.db_path(root)
    db.parent.mkdir(parents=True)
    _create_external_db(db)
    if os.name != "nt":
        db.chmod(0o644)

    conn = search_mod.connect(root)
    value = str(conn.execute("select value from sentinel").fetchone()[0])

    assert value == "external-preserved"
    if os.name != "nt":
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        for suffix in search_mod._SQLITE_SIDECAR_SUFFIXES:
            sidecar = Path(str(db) + suffix)
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    conn.close()
