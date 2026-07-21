from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return values
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            values[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return values


def _linux_memory() -> dict[str, Any] | None:
    path = Path("/proc/meminfo")
    if not path.exists():
        return None
    values: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        values[key] = value * 1024 if len(parts) > 1 and parts[1].lower() == "kb" else value
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return {
        "source": "proc_meminfo",
        "total_bytes": total,
        "available_bytes": available,
        "available_ratio": round(available / total, 6) if total else None,
        "swap_total_bytes": values.get("SwapTotal", 0),
        "swap_free_bytes": values.get("SwapFree", 0),
    }


def _darwin_memory() -> dict[str, Any] | None:
    if os.uname().sysname != "Darwin":
        return None
    try:
        total_proc = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
        vm_proc = subprocess.run(
            ["/usr/bin/vm_stat"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if total_proc.returncode != 0 or vm_proc.returncode != 0:
        return None
    try:
        total = int(total_proc.stdout.strip())
    except ValueError:
        return None
    page_size = 4096
    available_pages = 0
    for line in vm_proc.stdout.splitlines():
        if "page size of" in line:
            words = line.replace("bytes", "").split()
            for word in words:
                if word.isdigit():
                    page_size = int(word)
            continue
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        normalized = key.strip().lower()
        if normalized not in {
            "pages free",
            "pages inactive",
            "pages speculative",
            "pages purgeable",
        }:
            continue
        try:
            available_pages += int(raw.strip().rstrip("."))
        except ValueError:
            continue
    available = available_pages * page_size
    return {
        "source": "vm_stat",
        "total_bytes": total,
        "available_bytes": available,
        "available_ratio": round(available / total, 6) if total else None,
    }


def _generic_memory() -> dict[str, Any]:
    total = 0
    available = 0
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
        available = int(os.sysconf("SC_AVPHYS_PAGES")) * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return {
        "source": "sysconf" if total else "unavailable",
        "total_bytes": total,
        "available_bytes": available,
        "available_ratio": round(available / total, 6) if total else None,
    }


def _safe_cgroup_path(root: Path, raw: str) -> Path:
    relative = Path(raw.lstrip("/"))
    if any(part in {"", ".", ".."} for part in relative.parts):
        return root
    return root.joinpath(*relative.parts)


def _current_cgroup_directories(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> tuple[Path, Path]:
    v2 = cgroup_root
    v1 = cgroup_root / "memory"
    try:
        lines = proc_cgroup.read_text(encoding="ascii").splitlines()
    except OSError:
        return v2, v1
    for line in lines:
        hierarchy, separator, remainder = line.partition(":")
        if not separator:
            continue
        controllers, separator, raw_path = remainder.partition(":")
        if not separator:
            continue
        if hierarchy == "0" and controllers == "":
            v2 = _safe_cgroup_path(cgroup_root, raw_path)
        elif "memory" in {item.strip() for item in controllers.split(",")}:
            v1 = _safe_cgroup_path(cgroup_root / "memory", raw_path)
    return v2, v1


def _cgroup_memory(
    *,
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
    v2, v1 = _current_cgroup_directories(proc_cgroup=proc_cgroup, cgroup_root=cgroup_root)
    if (v2 / "memory.current").exists():
        current = _read_int(v2 / "memory.current")
        maximum = _read_int(v2 / "memory.max")
        events_path = v2 / "memory.events.local"
        if not events_path.exists():
            events_path = v2 / "memory.events"
        events = _read_key_values(events_path)
        return {
            "version": 2,
            "path": v2.as_posix(),
            "current_bytes": current,
            "max_bytes": maximum,
            "peak_bytes": _read_int(v2 / "memory.peak"),
            "usage_ratio": round(current / maximum, 6) if current is not None and maximum else None,
            "events": events,
        }
    if (v1 / "memory.usage_in_bytes").exists():
        current = _read_int(v1 / "memory.usage_in_bytes")
        maximum = _read_int(v1 / "memory.limit_in_bytes")
        failcnt = _read_int(v1 / "memory.failcnt")
        return {
            "version": 1,
            "path": v1.as_posix(),
            "current_bytes": current,
            "max_bytes": maximum,
            "peak_bytes": _read_int(v1 / "memory.max_usage_in_bytes"),
            "usage_ratio": round(current / maximum, 6) if current is not None and maximum else None,
            "events": {"failcnt": failcnt or 0},
        }
    return {
        "version": None,
        "path": None,
        "current_bytes": None,
        "max_bytes": None,
        "peak_bytes": None,
        "usage_ratio": None,
        "events": {},
    }


def system_memory_snapshot() -> dict[str, Any]:
    host = _linux_memory()
    if host is None:
        try:
            host = _darwin_memory()
        except AttributeError:  # os.uname absent on Windows
            host = None
    if host is None:
        host = _generic_memory()
    return {"host": host, "cgroup": _cgroup_memory()}


def _linux_process_tree_rss_kib(pid: int) -> int | None:
    root = Path(f"/proc/{pid}")
    if not root.exists():
        return None
    pending = [pid]
    seen: set[int] = set()
    total = 0
    while pending and len(seen) < 4096:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        status = Path(f"/proc/{current}/status")
        try:
            for line in status.read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            pass
        children = Path(f"/proc/{current}/task/{current}/children")
        try:
            pending.extend(int(item) for item in children.read_text(encoding="ascii").split())
        except (OSError, ValueError):
            pass
    return total


def _ps_process_tree_rss_kib(pid: int) -> int | None:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            child, parent, value = (int(item) for item in parts)
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)
        rss[child] = value
    pending = [pid]
    seen: set[int] = set()
    total = 0
    while pending and len(seen) < 4096:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        total += rss.get(current, 0)
        pending.extend(children.get(current, ()))
    return total if seen else None


def process_tree_rss_kib(pid: int) -> int | None:
    if pid <= 0:
        return None
    linux = _linux_process_tree_rss_kib(pid)
    if linux is not None:
        return linux
    return _ps_process_tree_rss_kib(pid)


def _signal_from_returncode(returncode: int | None, stderr: str) -> tuple[int | None, bool]:
    if returncode is None:
        return None, False
    if returncode < 0:
        return -returncode, False
    if returncode >= 128:
        mapped = returncode - 128
        if mapped in {item.value for item in signal.Signals} and (
            "killed" in stderr.lower() or "terminated" in stderr.lower() or mapped in {9, 15}
        ):
            return mapped, True
    return None, False


def _event_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int:
    before_events = ((before.get("cgroup") or {}).get("events") or {}) if isinstance(before, dict) else {}
    after_events = ((after.get("cgroup") or {}).get("events") or {}) if isinstance(after, dict) else {}
    try:
        return max(0, int(after_events.get(key, 0)) - int(before_events.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def classify_termination(
    *,
    returncode: int | None,
    timed_out: bool,
    before: dict[str, Any],
    after: dict[str, Any],
    peak_rss_kib: int | None,
    stderr: str = "",
) -> dict[str, Any]:
    signal_number, shell_mapped = _signal_from_returncode(returncode, stderr)
    signal_name: str | None = None
    if signal_number is not None:
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"SIG{signal_number}"

    evidence: list[str] = []
    classification = "success" if returncode == 0 and not timed_out else "nonzero_exit"
    confidence = "high"
    recommendations: list[str] = []
    if timed_out:
        classification = "timeout"
        evidence.append("runner_deadline_exceeded")
        recommendations.append("split the command into bounded batches or raise the explicit timeout")
    elif signal_number is not None:
        classification = "signal_termination"
        evidence.append(f"signal={signal_name}")
        if shell_mapped:
            evidence.append("shell_mapped_exit_code")
        if signal_number == signal.SIGKILL:
            oom_delta = _event_delta(before, after, "oom_kill")
            fail_delta = _event_delta(before, after, "failcnt")
            cgroup_after = after.get("cgroup") or {}
            usage_ratio = cgroup_after.get("usage_ratio")
            host_after = after.get("host") or {}
            available_ratio = host_after.get("available_ratio")
            if oom_delta > 0:
                classification = "cgroup_oom_kill_confirmed"
                evidence.append(f"cgroup_oom_kill_delta={oom_delta}")
            elif fail_delta > 0:
                classification = "cgroup_memory_limit_confirmed"
                evidence.append(f"cgroup_failcnt_delta={fail_delta}")
            elif isinstance(usage_ratio, (int, float)) and usage_ratio >= 0.95:
                classification = "cgroup_memory_limit_likely"
                confidence = "medium"
                evidence.append(f"cgroup_usage_ratio={usage_ratio}")
            elif isinstance(available_ratio, (int, float)) and available_ratio <= 0.03:
                classification = "host_memory_pressure_likely"
                confidence = "medium"
                evidence.append(f"host_available_ratio={available_ratio}")
            else:
                classification = "external_sigkill_or_execution_limit"
                confidence = "low"
                evidence.append("no_kernel_oom_evidence_observed")
            recommendations.extend(
                [
                    "inspect peak_rss_kib and cgroup memory.events",
                    "stream output and reduce worker parallelism before retrying",
                ]
            )
        elif signal_number == getattr(signal, "SIGXCPU", -1):
            classification = "cpu_limit_signal"
            recommendations.append("raise the CPU limit or split the workload")
    if peak_rss_kib is not None:
        evidence.append(f"peak_rss_kib={peak_rss_kib}")
    return {
        "classification": classification,
        "confidence": confidence,
        "returncode": returncode,
        "signal": signal_name,
        "signal_number": signal_number,
        "shell_mapped": shell_mapped,
        "evidence": evidence,
        "recommendations": recommendations,
    }


__all__ = ["classify_termination", "process_tree_rss_kib", "system_memory_snapshot"]
