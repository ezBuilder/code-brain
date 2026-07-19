from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ai_core import doctor
from ai_core import tracked_files as tracked


def test_strict_full_scan_bypasses_forged_tracked_list_cache(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "strict@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "strict"], cwd=repo, check=True)
    source = repo / "tracked.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
    tracked.tracked_files(repo)
    cache = repo / ".ai" / "cache" / "tracked-files.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["paths"] = []
    cache.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        cache.chmod(0o600)
    source.write_text("token=" + "z" * 24 + "\n", encoding="utf-8")

    check = doctor.check_secret_scan(repo, incremental=False, update_state=False)

    assert check.ok is False
    assert "tracked.py" in check.detail
    assert "mode=full" in check.detail