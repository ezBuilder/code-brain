from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_core import doctor


def test_doctor_mcp_registration_does_not_import_full_server(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "code-brain": {
                        "command": ".ai/bin/ai-mcp",
                        "args": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    for rel in doctor.REQUIRED_SLASH_COMMAND_FILES + doctor.REQUIRED_CODEX_PROMPT_FILES:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("managed\n", encoding="utf-8")
    previous = sys.modules.pop("ai_core.mcp_server", None)
    try:
        check = doctor.check_mcp_methods_registered(tmp_path)
        assert check.ok is True
        assert "mcp_methods=61" in check.detail
        assert "ai_core.mcp_server" not in sys.modules
    finally:
        if previous is not None:
            sys.modules["ai_core.mcp_server"] = previous