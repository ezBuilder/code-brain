"""Storage enforcement must not delete scratch files that tracked source needs.

Real incident: enforcement removed `.ai/tmp/<name>.mp4`, a 201MB SHA-256-pinned
fixture that a tracked live-gate script and an integration test referenced by
name. Neither size nor age distinguished it from disposable scratch, and the only
protection was a hand-placed `.keep` marker that did not exist, so routine cap
enforcement silently broke a verification gate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ai_core.storage_lifecycle import _managed_entries, _referenced_entry_names


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".ai" / "tmp").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text(".ai/\n", encoding="utf-8")
    return tmp_path


def _commit(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


def test_referenced_fixture_is_pinned(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "gate.py").write_text(
        'FIXTURE = ".ai/tmp/long_precision_204s.mp4"\nSHA = "abc123"\n', encoding="utf-8"
    )
    fixture = root / ".ai" / "tmp" / "long_precision_204s.mp4"
    fixture.write_bytes(b"x" * 1024)
    scratch = root / ".ai" / "tmp" / "disposable_scratch.log"
    scratch.write_bytes(b"y" * 1024)
    _commit(root)

    rows, _ = _managed_entries(root, root / ".ai" / "tmp")
    by_name = {row["name"]: row for row in rows}
    assert by_name["long_precision_204s.mp4"]["pinned"] is True
    assert by_name["disposable_scratch.log"]["pinned"] is False


def test_unreferenced_scratch_stays_collectable(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "README.md").write_text("nothing relevant here\n", encoding="utf-8")
    for name in ("alpha_output.bin", "beta_output.bin"):
        (root / ".ai" / "tmp" / name).write_bytes(b"z" * 512)
    _commit(root)

    rows, _ = _managed_entries(root, root / ".ai" / "tmp")
    assert all(row["pinned"] is False for row in rows), rows


def test_managed_directory_cannot_vouch_for_itself(tmp_path: Path) -> None:
    """A scratch file naming another scratch file must not protect it."""
    root = _repo(tmp_path)
    (root / "README.md").write_text("unrelated\n", encoding="utf-8")
    tmp_dir = root / ".ai" / "tmp"
    (tmp_dir / "target_artifact.bin").write_bytes(b"a" * 256)
    (tmp_dir / "manifest_note.txt").write_text("target_artifact.bin\n", encoding="utf-8")
    _commit(root)

    referenced = _referenced_entry_names(
        root, tmp_dir, ["target_artifact.bin", "manifest_note.txt"]
    )
    assert "target_artifact.bin" not in referenced


def test_short_or_extensionless_names_are_not_matched(tmp_path: Path) -> None:
    """Guard against over-pinning on common short tokens."""
    root = _repo(tmp_path)
    (root / "code.py").write_text("value = 'out'\nother = 'log'\n", encoding="utf-8")
    for name in ("out", "log", "a.b"):
        (root / ".ai" / "tmp" / name).write_bytes(b"q")
    _commit(root)

    referenced = _referenced_entry_names(root, root / ".ai" / "tmp", ["out", "log", "a.b"])
    assert referenced == set()


def test_missing_git_degrades_to_no_extra_pins(tmp_path: Path) -> None:
    """Without git the function must return empty, not raise."""
    plain = tmp_path / "plain"
    (plain / ".ai" / "tmp").mkdir(parents=True)
    (plain / ".ai" / "tmp" / "some_artifact.bin").write_bytes(b"w")
    assert _referenced_entry_names(plain, plain / ".ai" / "tmp", ["some_artifact.bin"]) == set()
