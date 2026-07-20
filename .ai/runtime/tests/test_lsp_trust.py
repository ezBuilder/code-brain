from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import lsp


@pytest.fixture(autouse=True)
def _reset_lsp_cache() -> None:
    lsp._cache_clear()
    yield
    lsp._cache_clear()


def _stub_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", True, raising=True)
    monkeypatch.setattr(lsp, "_detect_servers", lambda: ["pyright"], raising=True)


def _write_python(root: Path, rel: str = "pkg/a.py", *, lines: int = 4) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"value_{index} = {index}\n" for index in range(lines)),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("file_path", "line", "column", "reason"),
    [
        ("", 0, 0, "empty_file_path"),
        ("../outside.py", 0, 0, "file_path_outside_project"),
        ("/tmp/outside.py", 0, 0, "file_path_outside_project"),
        ("pkg/a.py\x00tail", 0, 0, "invalid_file_path_control_character"),
        ("pkg/a.js", 0, 0, "unsupported_language"),
        ("pkg/a.py", -1, 0, "invalid_position"),
        ("pkg/a.py", 0, -1, "invalid_position"),
    ],
)
def test_invalid_lsp_request_is_rejected_before_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_path: str,
    line: int,
    column: int,
    reason: str,
) -> None:
    _stub_available(monkeypatch)
    monkeypatch.setattr(
        lsp,
        "_lsp_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid request must not reach the language server")
        ),
    )

    refs = lsp.find_references(tmp_path, file_path, line, column)
    definition = lsp.goto_definition(tmp_path, file_path, line, column)

    assert refs["ok"] is False and refs["reason"] == reason
    assert definition["ok"] is False and definition["reason"] == reason


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_lsp_rejects_parent_symlink_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    external = tmp_path / "external"
    external.mkdir()
    outside = external / "a.py"
    outside.write_text("outside = True\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(
        lsp,
        "_lsp_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("linked source must not reach the language server")
        ),
    )

    payload = lsp.find_references(root, "pkg/a.py", 0, 0)

    assert payload == {"ok": False, "reason": "source_unavailable", "references": []}
    assert outside.read_text(encoding="utf-8") == "outside = True\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_lsp_rejects_hardlinked_source_without_external_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "pkg" / "a.py"
    source.parent.mkdir()
    external = tmp_path / "external.py"
    external.write_text("outside = True\n", encoding="utf-8")
    os.link(external, source)
    monkeypatch.setattr(
        lsp,
        "_lsp_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hardlinked source must not reach the language server")
        ),
    )

    payload = lsp.goto_definition(root, "pkg/a.py", 0, 0)

    assert payload == {"ok": False, "reason": "source_unavailable", "definition": None}
    assert external.read_text(encoding="utf-8") == "outside = True\n"


def test_lsp_rejects_position_outside_trusted_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    _write_python(tmp_path, lines=2)
    monkeypatch.setattr(
        lsp,
        "_lsp_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid position must not reach the language server")
        ),
    )

    past_line = lsp.find_references(tmp_path, "pkg/a.py", 2, 0)
    past_column = lsp.goto_definition(tmp_path, "pkg/a.py", 0, 10_000)

    assert past_line["reason"] == "invalid_position"
    assert past_column["reason"] == "invalid_position"


def test_lsp_filters_external_and_linked_result_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    _write_python(tmp_path, "pkg/a.py")
    internal = _write_python(tmp_path, "pkg/ref.py")
    external = tmp_path.parent / "outside-result.py"
    external.write_text("outside = True\n", encoding="utf-8")
    linked = tmp_path / "pkg" / "linked.py"
    if os.name != "nt":
        linked.symlink_to(external)
    raw = [
        {"absolutePath": str(external), "range": {"start": {"line": 0, "character": 0}}},
        {"relativePath": "pkg/ref.py", "range": {"start": {"line": 1, "character": 0}}},
    ]
    if os.name != "nt":
        raw.append(
            {"relativePath": "pkg/linked.py", "range": {"start": {"line": 0, "character": 0}}}
        )
    monkeypatch.setattr(lsp, "_lsp_call", lambda *_args, **_kwargs: raw)

    payload = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)

    assert payload["ok"] is True
    assert payload["references"] == [
        {
            "path": "pkg/ref.py",
            "line": 1,
            "column": 0,
            "preview": internal.read_text(encoding="utf-8").splitlines()[1],
        }
    ]


def test_lsp_reference_results_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_available(monkeypatch)
    _write_python(tmp_path, "pkg/a.py")
    _write_python(tmp_path, "pkg/ref.py")
    location = {
        "relativePath": "pkg/ref.py",
        "range": {"start": {"line": 0, "character": 0}},
    }
    monkeypatch.setattr(
        lsp,
        "_lsp_call",
        lambda *_args, **_kwargs: [location] * (lsp.LSP_RESULT_MAX + 50),
    )

    payload = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)

    assert payload["ok"] is True
    assert len(payload["references"]) == lsp.LSP_RESULT_MAX


def test_mcp_lsp_schemas_publish_basic_bounds() -> None:
    from ai_core import mcp_server

    by_name = {tool["name"]: tool for tool in mcp_server.TOOLS}
    for name in ("code_find_references", "code_goto_definition"):
        props = by_name[name]["inputSchema"]["properties"]
        assert props["file_path"]["maxLength"] == lsp.LSP_PATH_MAX_CHARS
        assert props["line"]["minimum"] == 0
        assert props["column"]["minimum"] == 0


def test_lsp_cache_deep_copies_put_and_get_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lsp.time, "monotonic", lambda: 100.0)
    key = ("root", "pkg/a.py", 1, 2)
    original = {"ok": True, "references": [{"path": "pkg/ref.py"}]}

    lsp._cache_put(key, original)
    original["references"][0]["path"] = "mutated-before-get.py"
    first = lsp._cache_get(key)

    assert first == {"ok": True, "references": [{"path": "pkg/ref.py"}]}
    assert first is not None
    first["references"][0]["path"] = "mutated-after-get.py"
    second = lsp._cache_get(key)
    assert second == {"ok": True, "references": [{"path": "pkg/ref.py"}]}
    assert second is not first


def test_lsp_cache_prunes_all_expired_entries_on_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(lsp.time, "monotonic", lambda: now[0])
    expired_key = ("root", "expired.py", 0, 0)
    live_key = ("root", "live.py", 0, 0)
    lsp._cache_put(expired_key, {"ok": True})
    now[0] = 4.0
    lsp._cache_put(live_key, {"ok": True})

    now[0] = 6.0
    assert lsp._cache_get(live_key) == {"ok": True}
    assert expired_key not in lsp._references_cache
    assert live_key in lsp._references_cache


def test_lsp_cache_enforces_fixed_entry_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lsp.time, "monotonic", lambda: 100.0)
    for index in range(lsp._CACHE_MAX_ENTRIES + 20):
        lsp._cache_put(
            ("root", f"pkg/{index}.py", index, 0),
            {"ok": True, "index": index},
        )

    assert len(lsp._references_cache) == lsp._CACHE_MAX_ENTRIES
    assert ("root", "pkg/0.py", 0, 0) not in lsp._references_cache
    newest = lsp._CACHE_MAX_ENTRIES + 19
    assert lsp._cache_get(("root", f"pkg/{newest}.py", newest, 0)) == {
        "ok": True,
        "index": newest,
    }


def test_lsp_cache_get_touches_lru_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lsp.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(lsp, "_CACHE_MAX_ENTRIES", 3)
    keys = [("root", f"pkg/{name}.py", 0, 0) for name in ("a", "b", "c")]
    for index, key in enumerate(keys):
        lsp._cache_put(key, {"index": index})

    assert lsp._cache_get(keys[0]) == {"index": 0}
    new_key = ("root", "pkg/d.py", 0, 0)
    lsp._cache_put(new_key, {"index": 3})

    assert keys[0] in lsp._references_cache
    assert keys[1] not in lsp._references_cache
    assert keys[2] in lsp._references_cache
    assert new_key in lsp._references_cache


def test_lsp_cache_invalidates_when_request_source_changes_within_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    source = _write_python(tmp_path, "pkg/a.py")
    calls = {"count": 0}

    def backend(*_args, **_kwargs):
        calls["count"] += 1
        return []

    monkeypatch.setattr(lsp, "_lsp_call", backend)

    first = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)
    original = source.stat()
    source.write_text("changed = 1\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)

    assert first["ok"] is True and second["ok"] is True
    assert calls["count"] == 2


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_lsp_cache_never_serves_request_source_replaced_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    source = _write_python(tmp_path, "pkg/a.py")
    external = tmp_path / "outside.py"
    external.write_text("outside = True\n", encoding="utf-8")
    calls = {"count": 0}

    def backend(*_args, **_kwargs):
        calls["count"] += 1
        return []

    monkeypatch.setattr(lsp, "_lsp_call", backend)

    assert lsp.find_references(tmp_path, "pkg/a.py", 0, 0)["ok"] is True
    source.unlink()
    source.symlink_to(external)
    second = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)

    assert second == {"ok": False, "reason": "source_unavailable", "references": []}
    assert calls["count"] == 1
    assert external.read_text(encoding="utf-8") == "outside = True\n"


def test_lsp_cache_invalidates_when_reference_preview_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    _write_python(tmp_path, "pkg/a.py")
    referenced = _write_python(tmp_path, "pkg/ref.py")
    calls = {"count": 0}
    location = {
        "relativePath": "pkg/ref.py",
        "range": {"start": {"line": 0, "character": 0}},
    }

    def backend(*_args, **_kwargs):
        calls["count"] += 1
        return [location]

    monkeypatch.setattr(lsp, "_lsp_call", backend)

    first = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)
    referenced.write_text("updated_reference = True\n", encoding="utf-8")
    second = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)

    assert calls["count"] == 2
    assert first["references"][0]["preview"] != second["references"][0]["preview"]
    assert second["references"][0]["preview"] == "updated_reference = True"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_lsp_cache_drops_reference_replaced_by_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_available(monkeypatch)
    _write_python(tmp_path, "pkg/a.py")
    referenced = _write_python(tmp_path, "pkg/ref.py")
    external = tmp_path / "outside-ref.py"
    external.write_text("outside = True\n", encoding="utf-8")
    calls = {"count": 0}
    location = {
        "relativePath": "pkg/ref.py",
        "range": {"start": {"line": 0, "character": 0}},
    }

    def backend(*_args, **_kwargs):
        calls["count"] += 1
        return [location]

    monkeypatch.setattr(lsp, "_lsp_call", backend)

    assert lsp.find_references(tmp_path, "pkg/a.py", 0, 0)["references"]
    referenced.unlink()
    os.link(external, referenced)
    second = lsp.find_references(tmp_path, "pkg/a.py", 0, 0)

    assert calls["count"] == 2
    assert second == {"ok": True, "references": []}
    assert external.read_text(encoding="utf-8") == "outside = True\n"
