from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def status(root: Path) -> dict[str, Any]:
    local = root / ".ai" / "cache" / "secrets.local"
    encrypted = sorted((root / ".ai" / "secrets").glob("*.enc.yaml"))
    return {
        "ok": True,
        "providers": {
            "os_keyring": False,
            "env": any(key.startswith("AI_SECRET_") for key in os.environ),
            "local_file": local.exists(),
            "sops_age_ciphertexts": [path.relative_to(root).as_posix() for path in encrypted],
        },
        "plaintext_tracked": False,
    }

