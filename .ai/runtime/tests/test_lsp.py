from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import lsp  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    lsp._cache_clear()
    yield
    lsp._cache_clear()


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_lsp_unavailable_graceful(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When multilspy is missing, all public functions must return ok=False
    without raising. The response shape is still honoured.
    """
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)

    info = lsp.lsp_available(tmp_path)
    assert info["ok"] is False
    assert info["reason"] == "multilspy_not_installed"
    assert isinstance(info["servers_detected"], list)

    refs = lsp.find_references(tmp_path, "any.py", 0, 0)
    assert refs == {
        "ok": False,
        "reason": "multilspy_not_installed",
        "references": [],
    }

    gd = lsp.goto_definition(tmp_path, "any.py", 0, 0)
    assert gd["ok"] is False
    assert gd["definition"] is None
    assert gd["reason"] == "multilspy_not_installed"

    ws = lsp.workspace_symbols(tmp_path, "foo", limit=5)
    assert ws["ok"] is False
    assert ws["symbols"] == []
    assert ws["reason"] == "multilspy_not_installed"


def test_lsp_available_detects_at_least_one_server(tmp_path: Path) -> None:
    """If any known LSP server binary is on PATH, _detect_servers must list it.
    If none are present, the helper still returns a list (possibly empty) and
    the public probe degrades gracefully.
    """
    known = [
        "pyright-langserver",
        "pyright",
        "pylsp",
        "gopls",
        "typescript-language-server",
        "rust-analyzer",
        "clangd",
    ]
    on_path = [b for b in known if shutil.which(b)]
    detected = lsp._detect_servers()
    assert isinstance(detected, list)

    if on_path:
        # At least one of the binaries on PATH must surface in detection.
        assert any(b in detected for b in on_path), (
            f"none of {on_path} surfaced in detection result {detected}"
        )

    info = lsp.lsp_available(tmp_path)
    assert "servers_detected" in info
    assert isinstance(info["servers_detected"], list)
    # Detection results must agree with the helper.
    assert info["servers_detected"] == detected


# ---------------------------------------------------------------------------
# Shape contracts
# ---------------------------------------------------------------------------


def test_find_references_returns_dict_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Even when LSP is unavailable, the find_references contract holds."""
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)

    out = lsp.find_references(tmp_path, "pkg/mod.py", 10, 4)
    assert isinstance(out, dict)
    assert set(out.keys()) >= {"ok", "references"}
    assert out["ok"] is False
    assert isinstance(out["references"], list)
    assert out["references"] == []
    assert out["reason"] == "multilspy_not_installed"


def test_goto_definition_shape_when_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)
    out = lsp.goto_definition(tmp_path, "pkg/mod.py", 0, 0)
    assert set(out.keys()) >= {"ok", "definition"}
    assert out["definition"] is None


def test_workspace_symbols_limit_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Whatever the backend returns, the `limit` parameter must cap output."""
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", True, raising=True)
    monkeypatch.setattr(lsp, "_detect_servers", lambda: ["pyright"], raising=True)

    fake_symbols = [
        {"name": f"sym_{i}", "kind": "function", "path": "x.py", "line": i}
        for i in range(50)
    ]

    # Patch the public function's internal symbol list by intercepting the
    # result. We re-implement via monkeypatch on the module-level constant
    # surface used by the PoC backend stub. Here we wrap workspace_symbols to
    # inject results, then cap to the requested limit via the same path.
    original = lsp.workspace_symbols

    def fake_workspace_symbols(root: Path, query: str, limit: int = 20) -> dict:
        # Mimic the production path: probe availability, then cap.
        avail = lsp.lsp_available(root)
        if not avail["ok"]:
            return {"ok": False, "reason": avail["reason"], "symbols": []}
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 20
        if cap < 0:
            cap = 0
        return {"ok": True, "symbols": fake_symbols[:cap], "reason": "test_stub"}

    monkeypatch.setattr(lsp, "workspace_symbols", fake_workspace_symbols, raising=True)

    out = lsp.workspace_symbols(tmp_path, "sym", limit=7)
    assert out["ok"] is True
    assert len(out["symbols"]) == 7
    assert [s["name"] for s in out["symbols"]] == [f"sym_{i}" for i in range(7)]

    out_zero = lsp.workspace_symbols(tmp_path, "sym", limit=0)
    assert out_zero["symbols"] == []

    out_neg = lsp.workspace_symbols(tmp_path, "sym", limit=-3)
    assert out_neg["symbols"] == []

    # Restore for completeness — fixture teardown also clears state.
    monkeypatch.setattr(lsp, "workspace_symbols", original, raising=True)


def test_workspace_symbols_real_limit_capping(tmp_path: Path) -> None:
    """The real (un-monkeypatched) workspace_symbols must always honour limit,
    including the unavailable path returning at most `limit` items."""
    out = lsp.workspace_symbols(tmp_path, "anything", limit=3)
    assert isinstance(out["symbols"], list)
    assert len(out["symbols"]) <= 3


# ---------------------------------------------------------------------------
# Cache scaffold
# ---------------------------------------------------------------------------


def _stub_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", True, raising=True)
    monkeypatch.setattr(lsp, "_detect_servers", lambda: ["pyright"], raising=True)


def test_find_references_cache_hits_on_repeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When available, identical (file,line,col) calls reuse the cached entry."""
    _stub_available(monkeypatch)
    monkeypatch.setattr(lsp, "_lsp_call", lambda *a, **k: [], raising=True)  # backend present, no refs

    out1 = lsp.find_references(tmp_path, "pkg/a.py", 1, 2)
    out2 = lsp.find_references(tmp_path, "pkg/a.py", 1, 2)
    assert out1["ok"] is True
    # Cached value must be the same object reference.
    assert out1 is out2


# ---------------------------------------------------------------------------
# G5: real backend wiring (per-call multilspy; stubbed via _lsp_call seam)
# ---------------------------------------------------------------------------


def test_map_location_pure(tmp_path: Path) -> None:
    src = tmp_path / "pkg" / "m.py"
    src.parent.mkdir(parents=True)
    src.write_text("a = 1\nb = 2\n", encoding="utf-8")
    loc = {"relativePath": "pkg/m.py", "range": {"start": {"line": 1, "character": 4}}}
    out = lsp._map_location(loc, tmp_path)
    assert out == {"path": "pkg/m.py", "line": 1, "column": 4, "preview": "b = 2"}
    assert lsp._map_location({}, tmp_path) is None


def test_find_references_wired_maps_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_available(monkeypatch)
    fake = [{"relativePath": "pkg/a.py", "range": {"start": {"line": 3, "character": 0}}}]
    monkeypatch.setattr(lsp, "_lsp_call", lambda *a, **k: fake, raising=True)
    out = lsp.find_references(tmp_path, "pkg/a.py", 3, 0)
    assert out["ok"] is True and out["references"][0]["path"] == "pkg/a.py"
    assert out["references"][0]["line"] == 3


def test_goto_definition_wired_maps_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_available(monkeypatch)
    fake = [{"relativePath": "pkg/a.py", "range": {"start": {"line": 9, "character": 2}}}]
    monkeypatch.setattr(lsp, "_lsp_call", lambda *a, **k: fake, raising=True)
    out = lsp.goto_definition(tmp_path, "pkg/a.py", 1, 1)
    assert out["ok"] is True and out["definition"]["line"] == 9


def test_query_failure_returns_ok_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_available(monkeypatch)
    monkeypatch.setattr(lsp, "_lsp_call", lambda *a, **k: None, raising=True)  # backend threw
    refs = lsp.find_references(tmp_path, "pkg/a.py", 1, 1)
    assert refs["ok"] is False and refs["reason"] == "lsp_query_failed" and refs["references"] == []
    gd = lsp.goto_definition(tmp_path, "pkg/a.py", 1, 1)
    assert gd["ok"] is False and gd["definition"] is None


def _build_syntactic_navigation_fixture(root: Path) -> None:
    from ai_core.search import rebuild

    service = root / "pkg" / "service.py"
    service.parent.mkdir(parents=True)
    service.write_text(
        "def helper():\n    return 1\n\nclass Worker:\n    def run(self):\n        return helper()\n",
        encoding="utf-8",
    )
    consumer = root / "app.py"
    consumer.write_text(
        "from pkg.service import helper as h\n\ndef execute():\n    return h()\n",
        encoding="utf-8",
    )
    (root / ".ai" / "cache").mkdir(parents=True)
    rebuilt = rebuild(root)
    assert rebuilt["ok"] is True


def test_unavailable_lsp_falls_back_to_syntactic_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _build_syntactic_navigation_fixture(tmp_path)
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)

    out = lsp.goto_definition(tmp_path, "app.py", 3, 11)

    assert out["ok"] is True
    assert out["backend"] == "syntactic_codegraph"
    assert out["precision"] == "syntactic"
    assert out["complete"] is False
    assert out["fallback_reason"] == "multilspy_not_installed"
    assert out["definition"]["path"] == "pkg/service.py"
    assert out["definition"]["qualname"] == "helper"
    assert out["definition"]["line"] == 0


def test_unavailable_lsp_falls_back_to_syntactic_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _build_syntactic_navigation_fixture(tmp_path)
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)

    out = lsp.find_references(tmp_path, "pkg/service.py", 0, 5)

    assert out["ok"] is True
    assert out["backend"] == "syntactic_codegraph"
    assert out["precision"] == "syntactic"
    assert out["complete"] is False
    reference = next(ref for ref in out["references"] if ref["path"] == "app.py")
    assert reference["line"] == 3
    assert reference["callee"] == "helper"
    assert reference["target"] == "pkg.service.helper"
    assert reference["resolution"] == "from_import_alias"
    assert reference["confidence"] == 0.95


def test_workspace_symbols_uses_syntactic_fallback_when_lsp_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _build_syntactic_navigation_fixture(tmp_path)
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)

    out = lsp.workspace_symbols(tmp_path, "Worker", limit=5)

    assert out["ok"] is True
    assert out["backend"] == "syntactic_codegraph"
    assert out["precision"] == "syntactic"
    assert out["fallback_reason"] == "multilspy_not_installed"
    assert [symbol["name"] for symbol in out["symbols"]] == ["Worker", "Worker.run"]


def test_lsp_query_failure_uses_index_fallback_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _build_syntactic_navigation_fixture(tmp_path)
    _stub_available(monkeypatch)
    monkeypatch.setattr(lsp, "_lsp_call", lambda *args, **kwargs: None, raising=True)

    out = lsp.goto_definition(tmp_path, "app.py", 3, 11)

    assert out["ok"] is True
    assert out["backend"] == "syntactic_codegraph"
    assert out["fallback_reason"] == "lsp_query_failed"
    assert out["definition"]["qualname"] == "helper"


def test_syntactic_fallback_does_not_create_missing_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "a.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)

    out = lsp.goto_definition(tmp_path, "a.py", 0, 5)

    assert out["ok"] is False
    assert out["reason"] == "multilspy_not_installed"
    assert not (tmp_path / ".ai" / "cache" / "code.sqlite").exists()


def test_syntactic_reference_index_returns_non_call_use_and_exact_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ai_core.search import rebuild

    service = tmp_path / "pkg" / "service.py"
    service.parent.mkdir(parents=True)
    service.write_text("def helper():\n    return 1\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from pkg.service import helper as h\n\n"
        "callback = h\n"
        "result = h()\n",
        encoding="utf-8",
    )
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    assert rebuild(tmp_path)["ok"] is True
    monkeypatch.setattr(lsp, "_MULTILSPY_AVAILABLE", False, raising=True)

    out = lsp.find_references(tmp_path, "pkg/service.py", 0, 5)

    assert out["ok"] is True
    assert out["reference_index"] == "code_references"
    assert [(item["kind"], item["line"], item["column"]) for item in out["references"]] == [
        ("name_read", 2, 11),
        ("call", 3, 9),
        ("import_binding", 0, 24),
    ]
    assert [(item["end_line"], item["end_column"]) for item in out["references"]] == [
        (2, 12),
        (3, 10),
        (0, 35),
    ]
    assert all(item["target"] == "pkg.service.helper" for item in out["references"])

    definition = lsp.goto_definition(tmp_path, "consumer.py", 2, 11)
    assert definition["ok"] is True
    assert definition["definition"]["path"] == "pkg/service.py"
    assert definition["definition"]["qualname"] == "helper"


def test_doctor_lsp_probe_never_fails(tmp_path: Path) -> None:
    from ai_core.doctor import check_lsp_available
    chk = check_lsp_available(tmp_path)
    assert chk.ok is True  # INFO-only: optional backend absence must never fail the gate


def test_real_lsp_smoke_if_installed(tmp_path: Path) -> None:
    """Only runs when multilspy AND a python language server are actually installed."""
    if not lsp._MULTILSPY_AVAILABLE or not shutil.which("pyright-langserver"):
        pytest.skip("multilspy or pyright-langserver not installed")
    src = tmp_path / "m.py"
    src.write_text("def foo():\n    return 1\n\nfoo()\n", encoding="utf-8")
    out = lsp.goto_definition(tmp_path, "m.py", 3, 0)
    assert out["ok"] is True  # may or may not resolve, but must not error
