"""ast_verify — neurosymbolic-lite policy gate (T31)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.ast_verify import verify_source, verify_file, Report  # noqa: E402


def test_safe_source_passes():
    src = '''
import os
import json
from pathlib import Path

def main():
    data = json.loads(Path("config.json").read_text())
    return data
'''
    rep = verify_source(src)
    assert rep.ok, [v.detail for v in rep.violations]


def test_forbidden_subprocess_call_blocked():
    src = '''
import os
def go():
    return os.system("rm -rf /")
'''
    rep = verify_source(src)
    assert not rep.ok
    assert any(v.kind == "call" and "os.system" in v.detail for v in rep.violations)


def test_forbidden_subprocess_import_blocked():
    src = 'import subprocess\nsubprocess.run(["ls"])\n'
    rep = verify_source(src)
    assert not rep.ok
    kinds = {v.kind for v in rep.violations}
    assert "import" in kinds
    # The call is also forbidden
    assert any("subprocess.run" in v.detail for v in rep.violations)


def test_forbidden_eval_blocked():
    rep = verify_source("x = eval('1+1')\n")
    assert not rep.ok
    assert any("eval" in v.detail for v in rep.violations)


def test_forbidden_compile_blocked():
    rep = verify_source("c = compile('x=1', 'm', 'exec')\n")
    assert not rep.ok
    assert any("compile" in v.detail for v in rep.violations)


def test_network_import_blocked():
    rep = verify_source("import urllib.request\n")
    assert not rep.ok
    assert any("urllib" in v.detail for v in rep.violations)


def test_globals_locals_flagged():
    rep = verify_source("print(globals())\n")
    assert not rep.ok
    assert any(v.kind == "name" and "globals" in v.detail for v in rep.violations)


def test_open_write_flagged():
    rep = verify_source("open('/tmp/x.txt', 'w').write('z')\n")
    assert not rep.ok
    assert any(v.kind == "open_write" for v in rep.violations)


def test_open_read_allowed():
    rep = verify_source("with open('/tmp/x.txt', 'r') as f: data = f.read()\n")
    # 'open' itself is fine when not in write mode; pathlib is preferred but not enforced
    # We allow read mode — only writes are flagged
    write_violations = [v for v in rep.violations if v.kind == "open_write"]
    assert not write_violations


def test_unlisted_third_party_import_blocked():
    rep = verify_source("import boto3\n")
    assert not rep.ok
    assert any("unlisted import" in v.detail for v in rep.violations)


def test_allow_imports_extension():
    rep = verify_source("import boto3\n", allow_imports={"boto3"})
    assert rep.ok


def test_ai_core_imports_always_allowed():
    rep = verify_source("from ai_core import memory\n")
    assert rep.ok


def test_syntax_error_reports_once():
    rep = verify_source("def foo(:\n")
    assert not rep.ok
    assert len(rep.violations) == 1
    assert rep.violations[0].kind == "syntax"


def test_report_to_dict_shape():
    rep = verify_source("eval('1')")
    d = rep.to_dict()
    assert "ok" in d and "violations" in d
    assert d["violations"][0]["kind"] == "call"
    assert "lineno" in d["violations"][0]


def test_verify_file_reads_real_module():
    """Smoke: our own ast_verify.py should pass."""
    from ai_core import ast_verify
    rep = verify_file(ast_verify.__file__)
    assert rep.ok or all(v.kind == "import" for v in rep.violations), \
        [v.detail for v in rep.violations]
