from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.process_janitor import cleanup_children, register_child, registry_path  # noqa: E402


def test_register_child_writes_redacted_shape(tmp_path: Path) -> None:
    register_child(tmp_path, pid=12345, kind="test", command=["ai", "index", "rebuild"])

    rows = [json.loads(line) for line in registry_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["pid"] == 12345
    assert rows[0]["kind"] == "test"
    assert rows[0]["command"] == ["ai", "index", "rebuild"]


def test_cleanup_children_terminates_stale_registered_child(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        register_child(tmp_path, pid=proc.pid, kind="sleep", command=["python", "sleep"])
        path = registry_path(tmp_path)
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        row["created_at"] = time.time() - 3600
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

        result = cleanup_children(tmp_path, ttl_seconds=1)

        assert result["killed"] == 1
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
