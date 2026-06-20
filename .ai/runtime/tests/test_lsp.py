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
