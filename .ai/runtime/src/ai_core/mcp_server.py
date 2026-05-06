from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .search import context_pack, query, rebuild
from .worker.ipc import health


def handle_request(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    try:
        if method == "memory_query" or method == "code_query":
            result = query(root, str(params.get("query", "")), limit=int(params.get("limit", 5)))
        elif method == "context_pack":
            result = context_pack(root, str(params.get("query", "")), limit=int(params.get("limit", 5)))
        elif method == "ai_status":
            result = health(root)
        elif method == "ai_request_rebuild":
            result = rebuild(root)
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}


def serve_stdio(root: Path) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        response = handle_request(root, json.loads(line))
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
    return 0

