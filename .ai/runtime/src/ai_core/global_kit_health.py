from __future__ import annotations

import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


KIT_ROOT = Path("kits/global-agent-kit")
_VALIDATE_PATH = Path("scripts/validate.sh")
_DOCTOR_PATH = Path("scripts/doctor.sh")
_MAX_CONTRACT_BYTES = 256 * 1024
_MAX_RULE_BYTES = 512 * 1024
_MANAGED_START = "<!-- code-brain-global-kit:start -->"
_MANAGED_END = "<!-- code-brain-global-kit:end -->"
_REQUIRED_FILES_RE = re.compile(r"(?ms)^required_files=\(\s*\n(?P<body>.*?)^\)\s*$")
_MANAGED_RULE_RE = re.compile(
    r'^check_managed_rule "\$ROOT_DIR/(?P<source>[^"]+)" "\$HOME/(?P<target>[^"]+)" "[^"]+"$'
)
_FILE_CHECK_RE = re.compile(r'^check_file "\$HOME/(?P<target>[^"]+)" "[^"]+"$')
_EXECUTABLE_CHECK_RE = re.compile(r'^check_executable "\$HOME/(?P<target>[^"]+)" "[^"]+"$')


@dataclass(frozen=True)
class GlobalKitHealth:
    ok: bool
    detail: str


@dataclass(frozen=True)
class GlobalKitInstallContract:
    managed_rules: tuple[tuple[PurePosixPath, PurePosixPath], ...]
    files: tuple[PurePosixPath, ...]
    executables: tuple[PurePosixPath, ...]


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe contract path: {value!r}")
    if any("\x00" in part for part in path.parts):
        raise ValueError("contract path contains NUL")
    return path


def _read_regular_text(path: Path, *, max_bytes: int, label: str) -> str:
    try:
        state = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} missing") from exc
    except OSError as exc:
        raise ValueError(f"{label} probe failed: {exc}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if state.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} unreadable: {exc}") from exc


def load_global_kit_source_inventory(root: Path) -> tuple[PurePosixPath, ...]:
    kit = root.resolve() / KIT_ROOT
    validate_text = _read_regular_text(
        kit / _VALIDATE_PATH,
        max_bytes=_MAX_CONTRACT_BYTES,
        label="global kit validate.sh",
    )
    match = _REQUIRED_FILES_RE.search(validate_text)
    if match is None:
        raise ValueError("global kit validate.sh required_files inventory missing")

    inventory: list[PurePosixPath] = [PurePosixPath("install.sh"), _VALIDATE_PATH]
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f"invalid required_files entry: {line!r}") from exc
        if len(tokens) != 1:
            raise ValueError(f"invalid required_files entry: {line!r}")
        inventory.append(_safe_relative(tokens[0]))

    deduped = tuple(dict.fromkeys(inventory))
    if len(deduped) < 3:
        raise ValueError("global kit source inventory is unexpectedly empty")
    return deduped


def check_global_kit_source(root: Path) -> GlobalKitHealth:
    kit = root.resolve() / KIT_ROOT
    try:
        inventory = load_global_kit_source_inventory(root)
    except ValueError as exc:
        return GlobalKitHealth(False, str(exc))

    issues: list[str] = []
    for relative in inventory:
        path = kit.joinpath(*relative.parts)
        try:
            state = path.lstat()
        except FileNotFoundError:
            issues.append(f"missing:{relative.as_posix()}")
            continue
        except OSError as exc:
            issues.append(f"probe:{relative.as_posix()}:{exc}")
            continue
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
            issues.append(f"non-regular:{relative.as_posix()}")

    if issues:
        return GlobalKitHealth(False, "; ".join(issues[:8]))
    return GlobalKitHealth(True, f"source inventory current; files={len(inventory)}")


def load_global_kit_install_contract(root: Path) -> GlobalKitInstallContract:
    kit = root.resolve() / KIT_ROOT
    doctor_text = _read_regular_text(
        kit / _DOCTOR_PATH,
        max_bytes=_MAX_CONTRACT_BYTES,
        label="global kit doctor.sh",
    )
    managed_rules: list[tuple[PurePosixPath, PurePosixPath]] = []
    files: list[PurePosixPath] = []
    executables: list[PurePosixPath] = []
    for raw_line in doctor_text.splitlines():
        line = raw_line.strip()
        managed = _MANAGED_RULE_RE.fullmatch(line)
        if managed is not None:
            managed_rules.append(
                (
                    _safe_relative(managed.group("source")),
                    _safe_relative(managed.group("target")),
                )
            )
            continue
        file_match = _FILE_CHECK_RE.fullmatch(line)
        if file_match is not None:
            files.append(_safe_relative(file_match.group("target")))
            continue
        executable_match = _EXECUTABLE_CHECK_RE.fullmatch(line)
        if executable_match is not None:
            executables.append(_safe_relative(executable_match.group("target")))

    contract = GlobalKitInstallContract(
        managed_rules=tuple(managed_rules),
        files=tuple(dict.fromkeys(files)),
        executables=tuple(dict.fromkeys(executables)),
    )
    if len(contract.managed_rules) != 2 or not contract.files or not contract.executables:
        raise ValueError(
            "global kit doctor.sh install inventory malformed: "
            f"managed={len(contract.managed_rules)} files={len(contract.files)} "
            f"executables={len(contract.executables)}"
        )
    return contract


def _bounded_regular_text_if_present(path: Path) -> str | None:
    try:
        state = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode) or state.st_size > _MAX_RULE_BYTES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _managed_rule_matches(source_text: str, target_text: str) -> bool:
    normalized_source = source_text.strip() + "\n"
    if target_text.strip() == normalized_source.strip():
        return True
    if _MANAGED_START not in target_text or _MANAGED_END not in target_text:
        return False
    before_end = target_text.split(_MANAGED_END, 1)[0]
    body = before_end.split(_MANAGED_START, 1)[1].lstrip("\n")
    return body == normalized_source


def _is_installed(
    kit: Path,
    home: Path,
    contract: GlobalKitInstallContract,
) -> bool:
    for source_relative, target_relative in contract.managed_rules:
        source_text = _bounded_regular_text_if_present(kit.joinpath(*source_relative.parts))
        target_text = _bounded_regular_text_if_present(home.joinpath(*target_relative.parts))
        if target_text is None:
            continue
        if _MANAGED_START in target_text or (
            source_text is not None and target_text.strip() == source_text.strip()
        ):
            return True

    # Settings alone are common on machines that do not use the kit. Treat only
    # kit-specific hooks/policies/skills as an installation signal.
    strong_files = [path for path in contract.files if path.as_posix() != ".claude/settings.json"]
    for relative in (*strong_files, *contract.executables):
        try:
            home.joinpath(*relative.parts).lstat()
        except OSError:
            continue
        return True
    return False


def _check_installed_regular(path: Path, *, executable: bool) -> str | None:
    try:
        state = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        return "non-regular"
    if executable and state.st_mode & 0o111 == 0:
        return "not-executable"
    return None


def check_global_kit_install(root: Path, *, home: Path | None = None) -> GlobalKitHealth:
    kit = root.resolve() / KIT_ROOT
    if home is None:
        home_value = os.environ.get("HOME")
        if not home_value:
            return GlobalKitHealth(True, "HOME unavailable; no installed kit contract asserted")
        active_home = Path(home_value).expanduser()
    else:
        active_home = home
    active_home = active_home.resolve()

    try:
        contract = load_global_kit_install_contract(root)
    except ValueError as exc:
        return GlobalKitHealth(False, str(exc))
    if not _is_installed(kit, active_home, contract):
        return GlobalKitHealth(True, "global kit not installed; drift check not applicable")

    issues: list[str] = []
    for source_relative, target_relative in contract.managed_rules:
        try:
            source_text = _read_regular_text(
                kit.joinpath(*source_relative.parts),
                max_bytes=_MAX_RULE_BYTES,
                label=f"source rule {source_relative.as_posix()}",
            )
        except ValueError as exc:
            issues.append(str(exc))
            continue
        target_path = active_home.joinpath(*target_relative.parts)
        target_text = _bounded_regular_text_if_present(target_path)
        if target_text is None:
            issues.append(f"managed-rule:{target_relative.as_posix()}:missing-or-non-regular")
            continue
        if not _managed_rule_matches(source_text, target_text):
            issues.append(f"managed-rule:{target_relative.as_posix()}:drift")

    for relative in contract.files:
        state = _check_installed_regular(active_home.joinpath(*relative.parts), executable=False)
        if state is not None:
            issues.append(f"file:{relative.as_posix()}:{state}")
    for relative in contract.executables:
        state = _check_installed_regular(active_home.joinpath(*relative.parts), executable=True)
        if state is not None:
            issues.append(f"executable:{relative.as_posix()}:{state}")

    if issues:
        return GlobalKitHealth(False, "; ".join(issues[:8]))
    return GlobalKitHealth(
        True,
        (
            f"installed contract current; managed={len(contract.managed_rules)} "
            f"files={len(contract.files)} executables={len(contract.executables)}"
        ),
    )
