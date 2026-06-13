"""tmux adapter abstraction (PRD §5.1, §10) — list/send/capture/restart.

FakeTmuxAdapter is the deterministic test/dev double used by loopd's unit tests and the
no-tmux dev path. TmuxAdapter is the real send-keys/capture-pane implementation; it is
constructed lazily and only used when an actual tmux server is present. Neither adapter
ever injects or echoes secret values — task text only. stdlib only.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from typing import Any

_PANE_RE = re.compile(r"^%\d+$")  # only tmux pane ids; reject session:window targets


class TmuxAdapterBase:
    def available(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def pane_alive(self, pane_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def inject(self, pane_id: str, text: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def capture(self, pane_id: str) -> str:  # pragma: no cover
        raise NotImplementedError


class FakeTmuxAdapter(TmuxAdapterBase):
    """In-memory double: records injected task text and serves scripted pane output."""

    def __init__(self, alive: set[str] | None = None) -> None:
        self._alive = set(alive or set())
        self.injected: list[dict[str, str]] = []
        self._output: dict[str, str] = {}

    def add_pane(self, pane_id: str) -> None:
        self._alive.add(pane_id)

    def set_output(self, pane_id: str, text: str) -> None:
        self._output[pane_id] = text

    def available(self) -> bool:
        return True

    def pane_alive(self, pane_id: str) -> bool:
        return pane_id in self._alive

    def inject(self, pane_id: str, text: str) -> bool:
        if pane_id not in self._alive:
            return False
        self.injected.append({"pane_id": pane_id, "text": text})
        return True

    def capture(self, pane_id: str) -> str:
        return self._output.get(pane_id, "")

    def new_window(self, session: str, window: str, env: dict[str, str], command: str) -> str | None:
        pane = f"%{len(self.injected) + len(self._alive) + 100}"
        self._alive.add(pane)
        self.launched = getattr(self, "launched", [])
        self.launched.append({"session": session, "window": window, "env": dict(env),
                              "command": command, "pane_id": pane})
        return pane


class TmuxAdapter(TmuxAdapterBase):
    """Real tmux via send-keys / capture-pane / list-panes. Read+inject; never spawns logins."""

    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout
        self._bin = shutil.which("tmux")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        if not self._bin:
            return None
        try:
            return subprocess.run([self._bin, *args], capture_output=True, text=True,
                                  timeout=self._timeout, check=False, shell=False)
        except (subprocess.TimeoutExpired, OSError):
            return None

    def available(self) -> bool:
        proc = self._run("list-sessions")
        return proc is not None and proc.returncode == 0

    def pane_alive(self, pane_id: str) -> bool:
        proc = self._run("list-panes", "-a", "-F", "#{pane_id}")
        if proc is None or proc.returncode != 0:
            return False
        return pane_id in (proc.stdout or "").split()

    def inject(self, pane_id: str, text: str) -> bool:
        if not _PANE_RE.fullmatch(str(pane_id)):
            return False  # never target a non-pane (e.g. someone else's session:window)
        if not self.pane_alive(pane_id):
            return False
        # send the literal task text, then Enter — text only, never secrets
        sent = self._run("send-keys", "-t", pane_id, "-l", text)
        if sent is None or sent.returncode != 0:
            return False
        enter = self._run("send-keys", "-t", pane_id, "Enter")
        return enter is not None and enter.returncode == 0

    def capture(self, pane_id: str) -> str:
        proc = self._run("capture-pane", "-p", "-t", pane_id)
        return (proc.stdout if proc and proc.returncode == 0 else "") or ""

    def new_window(self, session: str, window: str, env: dict[str, str], command: str) -> str | None:
        """Create (or reuse) a session and open a window running `command` under `env`.

        env holds ONLY directory paths (HOME/XDG/CODE_BRAIN_*), never secrets. Each `-e k=v`
        is a separate argv element (no shell). `command` is a fixed binary name validated by
        the caller. Returns the new pane id or None.
        """
        if not self._bin:
            return None
        has = self._run("has-session", "-t", session)
        env_args: list[str] = []
        for k, v in env.items():
            env_args += ["-e", f"{k}={v}"]
        if has is None or has.returncode != 0:
            created = self._run("new-session", "-d", "-s", session, "-n", window, *env_args, command)
        else:
            created = self._run("new-window", "-t", session, "-n", window, *env_args, command)
        if created is None or created.returncode != 0:
            return None
        # resolve the pane id of the just-created window
        got = self._run("display-message", "-p", "-t", f"{session}:{window}", "#{pane_id}")
        if got is None or got.returncode != 0:
            return None
        pane = (got.stdout or "").strip().split("\n")[0]
        return pane if _PANE_RE.fullmatch(pane) else None


def output_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def get_adapter(prefer_real: bool = True) -> TmuxAdapterBase:
    """Real adapter when a tmux server is reachable, else the fake double (dev/no-tmux)."""
    if prefer_real:
        real = TmuxAdapter()
        if real.available():
            return real
    return FakeTmuxAdapter()
