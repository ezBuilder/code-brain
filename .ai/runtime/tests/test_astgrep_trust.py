from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_core import astgrep_integration as ag
from ai_core import mcp_server
from ai_core import search as search_mod


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ai").mkdir()
    (root / ".ai" / "config.yaml").write_text(
        "project_name: ast-search\n",
        encoding="utf-8",
    )
    return root


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("", "empty pattern"),
        ("x\x00y", "invalid pattern control character"),
        ("x" * (ag.AST_PATTERN_MAX_CHARS + 1), "pattern too long"),
    ],
)
def test_ast_search_rejects_invalid_pattern_before_binary_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
    reason: str,
) -> None:
    monkeypatch.setattr(
        ag,
        "astgrep_available",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid pattern must not probe ast-grep")
        ),
    )

    payload = ag.ast_grep_search(tmp_path, pattern=pattern, lang="python")

    assert payload == {"ok": False, "reason": reason, "matches": []}


@pytest.mark.skipif(os.name == "nt", reason="Unix directory link semantics")
def test_ast_search_rejects_linked_scope_without_scanning_external_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    outside = external / "outside.py"
    outside.write_text("outside_call()\n", encoding="utf-8")
    (root / "linked").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(ag, "astgrep_available", lambda: True)
    monkeypatch.setattr(
        ag,
        "scan_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("linked scope must not reach ast-grep")
        ),
    )

    payload = ag.ast_grep_search(
        root,
        pattern="$F()",
        lang="python",
        path="linked",
    )

    assert payload == {"ok": False, "reason": "path unavailable", "matches": []}
    assert outside.read_text(encoding="utf-8") == "outside_call()\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_ast_search_rejects_hardlinked_file_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_repo(tmp_path)
    external = tmp_path / "outside.py"
    external.write_text("outside_call()\n", encoding="utf-8")
    scoped = root / "src" / "scoped.py"
    scoped.parent.mkdir()
    os.link(external, scoped)
    monkeypatch.setattr(ag, "astgrep_available", lambda: True)
    monkeypatch.setattr(
        ag,
        "scan_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hardlinked scope must not reach ast-grep")
        ),
    )

    payload = ag.ast_grep_search(
        root,
        pattern="$F()",
        lang="python",
        path="src/scoped.py",
    )

    assert payload == {"ok": False, "reason": "path unavailable", "matches": []}
    assert external.read_text(encoding="utf-8") == "outside_call()\n"


def test_ast_search_scans_only_trusted_language_files_in_private_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_repo(tmp_path)
    good = _write(root, "src/good.py", "def good():\n    return 1\n")
    _write(root, "src/other.js", "function other() {}\n")
    _write(root, "node_modules/pkg/hidden.py", "def hidden(): pass\n")
    captured: dict[str, object] = {}

    def fake_scan(target: Path, _rule: str, *, timeout_seconds: float):
        target = Path(target)
        copied = sorted(
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file()
        )
        captured["copied"] = copied
        captured["timeout"] = timeout_seconds
        mirrored = target / "src" / "good.py"
        assert mirrored.read_text(encoding="utf-8") == good.read_text(encoding="utf-8")
        return [
            {
                "file": str(mirrored),
                "range": {"start": {"line": 0}},
                "text": "def good():",
            }
        ]

    monkeypatch.setattr(ag, "astgrep_available", lambda: True)
    monkeypatch.setattr(ag, "scan_path", fake_scan)

    payload = ag.ast_grep_search(
        root,
        pattern="def $NAME(): $$$",
        lang="python",
        timeout_seconds=10**9,
    )

    assert captured["copied"] == ["src/good.py"]
    assert captured["timeout"] == ag.AST_TIMEOUT_MAX_SECONDS
    assert payload == {
        "ok": True,
        "count": 1,
        "lang": "python",
        "matches": [{"file": "src/good.py", "line": 1, "text": "def good():"}],
    }


def test_ast_search_caps_results_and_rejects_partial_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_repo(tmp_path)
    _write(root, "src/a.py", "a = 1\n")
    _write(root, "src/b.py", "b = 2\n")
    monkeypatch.setattr(ag, "astgrep_available", lambda: True)
    monkeypatch.setattr(ag, "AST_MATERIALIZE_MAX_FILES", 1)
    monkeypatch.setattr(
        ag,
        "scan_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("partial mirror must not be scanned")
        ),
    )

    overflow = ag.ast_grep_search(root, pattern="$X = $Y", lang="python")

    assert overflow == {"ok": False, "reason": "search scope too large", "matches": []}

    monkeypatch.setattr(ag, "AST_MATERIALIZE_MAX_FILES", 10)

    def many_findings(target: Path, _rule: str, *, timeout_seconds: float):
        mirrored = Path(target) / "src" / "a.py"
        return [
            {
                "file": str(mirrored),
                "range": {"start": {"line": index}},
                "text": f"match-{index}",
            }
            for index in range(ag.AST_RESULT_MAX + 25)
        ]

    monkeypatch.setattr(ag, "scan_path", many_findings)
    capped = ag.ast_grep_search(
        root,
        pattern="$X = $Y",
        lang="python",
        max_results=10**9,
    )
    assert capped["count"] == ag.AST_RESULT_MAX
    assert len(capped["matches"]) == ag.AST_RESULT_MAX


def test_scan_path_uses_bounded_process_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source.py"
    target.write_text("value = 1\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_reader(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return [json.dumps({"file": str(target), "text": "value = 1"})]

    monkeypatch.setattr(ag, "_binary", lambda: "/usr/bin/ast-grep")
    monkeypatch.setattr(search_mod, "_run_process_lines_bounded", fake_reader)

    findings = ag.scan_path(target, "id: x\nlanguage: Python\nrule:\n  pattern: $X\n")

    assert findings == [{"file": str(target), "text": "value = 1"}]
    assert captured["max_output_bytes"] == ag.AST_OUTPUT_MAX_BYTES
    assert captured["max_events"] == ag.AST_OUTPUT_MAX_EVENTS
    assert captured["require_complete"] is True
    assert captured["command"][-1] == str(target)


def test_scan_path_rejects_oversized_rule_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(ag, "_binary", lambda: "/usr/bin/ast-grep")
    monkeypatch.setattr(
        search_mod,
        "_run_process_lines_bounded",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized rule must not spawn ast-grep")
        ),
    )

    assert ag.scan_path(target, "x" * (ag.AST_RULE_MAX_CHARS + 1)) == []


def test_internal_extractors_delegate_to_bounded_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source.js"
    target.write_text("function hello() {}\n", encoding="utf-8")
    monkeypatch.setattr(ag, "_binary", lambda: "/usr/bin/ast-grep")
    calls: list[Path] = []

    def fake_scan(path: Path, _rule: str, *, timeout_seconds: float):
        calls.append(Path(path))
        return [
            {
                "message": "function",
                "matches": [
                    {
                        "start": {"line": 0},
                        "end": {"line": 0},
                        "text": "function hello() {}",
                    }
                ],
            }
        ]

    monkeypatch.setattr(ag, "scan_path", fake_scan)

    symbols = ag.extract_symbols_js(str(target))

    assert calls == [target]
    assert symbols[0]["qualname"] == "hello"


def test_ast_grep_mcp_schema_matches_runtime_bounds() -> None:
    by_name = {tool["name"]: tool for tool in mcp_server.TOOLS}
    props = by_name["ast_grep_search"]["inputSchema"]["properties"]
    assert props["pattern"]["maxLength"] == ag.AST_PATTERN_MAX_CHARS
    assert props["path"]["maxLength"] == ag.AST_PATH_MAX_CHARS
    assert props["max_results"]["minimum"] == 1
    assert props["max_results"]["maximum"] == ag.AST_RESULT_MAX
