from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def _require_root_confined_parent(path: Path, root: Path | None) -> None:
    if root is None:
        return
    try:
        path.parent.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError) as exc:
        raise OSError("private write parent escapes project root") from exc


def _confined_parent_parts(path: Path, root: Path) -> tuple[Path, tuple[str, ...]]:
    root_absolute = Path(os.path.abspath(root))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative_parent = path_absolute.parent.relative_to(root_absolute)
    except ValueError as exc:
        raise OSError("private write parent escapes project root") from exc
    parts = tuple(part for part in relative_parent.parts if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise OSError("private write parent escapes project root")
    return root_absolute, parts


def _raise_confined_path_error(exc: OSError) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise OSError("private path contains symlink or escapes project root") from exc
    if exc.errno in {errno.ENXIO, errno.ENODEV}:
        raise OSError("private mutation target is not a regular file") from exc
    raise exc


def _require_safe_directory_fd(fd: int) -> None:
    state = os.fstat(fd)
    if not stat.S_ISDIR(state.st_mode):
        raise OSError("private path component is not a directory")
    if os.name != "nt":
        if stat.S_IMODE(state.st_mode) & 0o022:
            raise PermissionError("private path component is group/other writable")
        if hasattr(os, "geteuid") and state.st_uid != os.geteuid():
            raise PermissionError("private path component owner mismatch")


@contextmanager
def _open_confined_parent_fd(
    path: Path,
    *,
    root: Path,
    create: bool,
) -> Iterator[int]:
    """Open each parent component from root without following directory links."""
    root_absolute, parts = _confined_parent_parts(path, root)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if create:
        root_absolute.mkdir(parents=True, exist_ok=True)
    current_fd = os.open(root_absolute, directory_flags)
    try:
        for part in parts:
            child_flags = directory_flags
            if hasattr(os, "O_NOFOLLOW"):
                child_flags |= os.O_NOFOLLOW
            try:
                child_fd = os.open(part, child_flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(part, child_flags, dir_fd=current_fd)
                except OSError as exc:
                    _raise_confined_path_error(exc)
            except OSError as exc:
                _raise_confined_path_error(exc)
            _require_safe_directory_fd(child_fd)
            os.close(current_fd)
            current_fd = child_fd
        yield current_fd
    finally:
        try:
            os.close(current_fd)
        except OSError:
            pass


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("write made no progress")
        remaining = remaining[written:]


def _open_or_create_private_file(
    parent_fd: int,
    name: str,
    flags: int,
    *,
    retries: int = 4,
) -> int:
    """Race-safe open for a shared private file under an already-confined parent."""
    existing_flags = flags & ~os.O_CREAT
    create_flags = existing_flags | os.O_CREAT | os.O_EXCL
    last_error: OSError | None = None
    for _attempt in range(max(1, retries)):
        try:
            return os.open(name, create_flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            try:
                return os.open(name, existing_flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                # Another actor replaced the directory entry between the two
                # operations. Retry against the same confined parent fd.
                last_error = exc
                continue
        except FileNotFoundError as exc:
            # Some platforms transiently report ENOENT for concurrent first
            # creation with O_NOFOLLOW. A bounded retry is safe and deterministic.
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise OSError("unable to open or create private file")


def _require_single_link(state: os.stat_result) -> None:
    if int(getattr(state, "st_nlink", 1)) != 1:
        raise OSError("private file has multiple hard links")


def _require_private_mutation_target(state: os.stat_result) -> None:
    if not stat.S_ISREG(state.st_mode):
        raise OSError("private mutation target is not a regular file")
    _require_single_link(state)
    if os.name != "nt" and hasattr(os, "geteuid") and state.st_uid != os.geteuid():
        raise PermissionError("private mutation target owner mismatch")


def ensure_root_confined_directory(
    path: Path,
    *,
    root: Path,
    mode: int = 0o700,
) -> Path:
    """Create/validate a non-symlink directory confined under *root*."""
    path = Path(path)
    root = Path(root)
    if os.name != "nt":
        with _open_confined_parent_fd(path, root=root, create=True) as parent_fd:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                os.mkdir(path.name, mode, dir_fd=parent_fd)
                directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                state = os.fstat(directory_fd)
                if not stat.S_ISDIR(state.st_mode):
                    raise OSError("private directory is not a regular directory")
                _require_safe_directory_fd(directory_fd)
                if hasattr(os, "fchmod"):
                    os.fchmod(directory_fd, mode)
            finally:
                os.close(directory_fd)
        return path
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing symlink directory")
    path.mkdir(parents=True, exist_ok=True)
    try:
        state = path.lstat()
        if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode):
            raise OSError("private directory is not a regular directory")
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise OSError("private directory escapes project root") from exc
    if os.name != "nt":
        path.chmod(mode)
    return path


def validate_root_confined_directory(
    path: Path,
    *,
    root: Path,
    require_safe_permissions: bool = True,
) -> os.stat_result:
    """Validate an existing directory through root-relative no-follow fds."""
    path = Path(path)
    root = Path(root)
    if os.name != "nt":
        with _open_confined_parent_fd(path, root=root, create=False) as parent_fd:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                state = os.fstat(directory_fd)
                if not stat.S_ISDIR(state.st_mode):
                    raise OSError("private path component is not a directory")
                if require_safe_permissions:
                    _require_safe_directory_fd(directory_fd)
                return state
            finally:
                os.close(directory_fd)
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing symlink directory")
    state = path.stat()
    if not stat.S_ISDIR(state.st_mode):
        raise OSError("private path component is not a directory")
    return state


def list_root_confined_directory(
    path: Path,
    *,
    root: Path,
    max_entries: int = 1_000,
    require_safe_permissions: bool = True,
) -> list[str]:
    """List one confined directory through a no-follow descriptor with an entry cap."""
    path = Path(path)
    root = Path(root)
    cap = max(0, int(max_entries))
    if os.name != "nt":
        with _open_confined_parent_fd(path, root=root, create=False) as parent_fd:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                state = os.fstat(directory_fd)
                if not stat.S_ISDIR(state.st_mode):
                    raise OSError("directory listing target is not a directory")
                if require_safe_permissions:
                    _require_safe_directory_fd(directory_fd)
                names: list[str] = []
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        if len(names) >= cap:
                            raise OSError("directory listing exceeds entry limit")
                        names.append(entry.name)
                return sorted(names)
            finally:
                os.close(directory_fd)
    validate_root_confined_directory(
        path,
        root=root,
        require_safe_permissions=require_safe_permissions,
    )
    names = []
    with os.scandir(path) as entries:
        for entry in entries:
            if len(names) >= cap:
                raise OSError("directory listing exceeds entry limit")
            names.append(entry.name)
    return sorted(names)


def remove_root_confined_tree(path: Path, *, root: Path) -> bool:
    """Remove one confined directory tree without following directory links.

    Parent components and the final entry are resolved through no-follow file
    descriptors on POSIX. The fd-based ``shutil.rmtree`` implementation unlinks
    child symlinks instead of traversing them. Returns ``False`` when absent.
    """
    path = Path(path)
    root = Path(root)
    if os.name != "nt":
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            raise OSError("secure recursive removal is unavailable")
        try:
            with _open_confined_parent_fd(path, root=root, create=False) as parent_fd:
                try:
                    state = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode):
                    raise OSError("recursive removal target is not a regular directory")
                shutil.rmtree(path.name, dir_fd=parent_fd)
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
                return True
        except FileNotFoundError:
            return False
    _require_root_confined_parent(path, root)
    try:
        state = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode):
        raise OSError("recursive removal target is not a regular directory")
    shutil.rmtree(path)
    return True


def validate_root_confined_regular_file(
    path: Path,
    *,
    root: Path,
    min_bytes: int = 0,
    max_bytes: int | None = None,
    require_owner: bool = False,
    reject_group_other_writable: bool = False,
) -> os.stat_result:
    """Validate a confined regular single-link file through a no-follow descriptor."""
    path = Path(path)
    root = Path(root)
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if os.name != "nt":
        with _open_confined_parent_fd(path, root=root, create=False) as parent_fd:
            try:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                state = os.fstat(fd)
                if not stat.S_ISREG(state.st_mode):
                    raise OSError("target is not a regular file")
                _require_single_link(state)
                mode = stat.S_IMODE(state.st_mode)
                if reject_group_other_writable and mode & 0o022:
                    raise PermissionError("target is group/other writable")
                if require_owner and hasattr(os, "geteuid") and state.st_uid != os.geteuid():
                    raise PermissionError("target owner mismatch")
                if state.st_size < max(0, int(min_bytes)):
                    raise OSError("target is smaller than required")
                if max_bytes is not None and state.st_size > max(0, int(max_bytes)):
                    raise OSError("target exceeds size limit")
                return state
            finally:
                os.close(fd)
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing symlink launcher")
    state = path.stat()
    if not stat.S_ISREG(state.st_mode):
        raise OSError("target is not a regular file")
    _require_single_link(state)
    if state.st_size < max(0, int(min_bytes)):
        raise OSError("target is smaller than required")
    if max_bytes is not None and state.st_size > max(0, int(max_bytes)):
        raise OSError("target exceeds size limit")
    return state


def ensure_root_confined_private_regular_file(
    path: Path,
    *,
    root: Path,
    replace_unsafe: bool = False,
) -> os.stat_result:
    """Create or privatize one confined regular single-link file in place.

    Existing trusted content is preserved. When ``replace_unsafe`` is true, an
    unsafe final entry is atomically replaced with an empty 0600 file without
    following or mutating its target.
    """
    path = Path(path)
    root = Path(root)

    def ensure_once() -> os.stat_result:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if os.name != "nt":
            with _open_confined_parent_fd(path, root=root, create=True) as parent_fd:
                try:
                    fd = _open_or_create_private_file(parent_fd, path.name, flags)
                except OSError as exc:
                    _raise_confined_path_error(exc)
                try:
                    _require_private_mutation_target(os.fstat(fd))
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                    return os.fstat(fd)
                finally:
                    os.close(fd)
        _require_root_confined_parent(path, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("refusing symlink private file")
        fd = os.open(path, flags, 0o600)
        try:
            _require_private_mutation_target(os.fstat(fd))
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            return os.fstat(fd)
        finally:
            os.close(fd)

    try:
        return ensure_once()
    except (OSError, PermissionError):
        if not replace_unsafe:
            raise
    atomic_write_private_bytes(path, b"", root=root)
    return ensure_once()


def validate_root_confined_executable(path: Path, *, root: Path) -> os.stat_result:
    """Validate an owner-controlled regular single-link launcher without following links."""
    state = validate_root_confined_regular_file(
        path,
        root=root,
        require_owner=True,
        reject_group_other_writable=True,
    )
    if os.name != "nt" and stat.S_IMODE(state.st_mode) & 0o111 == 0:
        raise PermissionError("launcher is not executable")
    return state


def atomic_write_private_bytes(
    path: Path,
    data: bytes | bytearray | memoryview,
    *,
    root: Path | None = None,
) -> None:
    """Atomically replace *path* with bytes created as mode 0600.

    The temporary file is private before the first byte is written. Replacing an
    existing symlink replaces the link itself and never follows its target.
    """
    path = Path(path)
    encoded = bytes(data)
    if root is not None and os.name != "nt":
        with _open_confined_parent_fd(path, root=Path(root), create=True) as parent_fd:
            temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                _write_all(fd, encoded)
                os.fsync(fd)
                os.close(fd)
                fd = -1
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        return
    _require_root_confined_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_private_text(path: Path, text: str, *, root: Path | None = None) -> None:
    """Atomically replace *path* with UTF-8 text created as mode 0600."""
    atomic_write_private_bytes(path, text.encode("utf-8"), root=root)


def append_private_text(path: Path, text: str, *, root: Path | None = None) -> None:
    """Append one UTF-8 record to a private regular file without following links."""
    path = Path(path)
    if root is not None and os.name != "nt":
        with _open_confined_parent_fd(path, root=Path(root), create=True) as parent_fd:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = _open_or_create_private_file(parent_fd, path.name, flags)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                _require_private_mutation_target(os.fstat(fd))
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                encoded = text.encode("utf-8")
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                _write_all(fd, encoded)
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(fd)
        return
    _require_root_confined_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("refusing to append through symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        _require_private_mutation_target(os.fstat(fd))
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        encoded = text.encode("utf-8")
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("append made no progress")
            remaining = remaining[written:]
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
    if os.name != "nt":
        path.chmod(0o600)


def read_root_confined_bytes(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int = 1_000_000,
    require_private: bool = True,
    require_owner: bool = False,
    reject_group_other_writable: bool = False,
) -> tuple[bytes, os.stat_result]:
    """Read bounded bytes from a regular single-link file without following links."""
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_context = (
        _open_confined_parent_fd(path, root=Path(root), create=False)
        if root is not None and os.name != "nt"
        else None
    )
    if parent_context is not None:
        with parent_context as parent_fd:
            try:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            return _read_open_bytes_fd(
                fd,
                max_bytes=max_bytes,
                require_private=require_private,
                require_owner=require_owner,
                reject_group_other_writable=reject_group_other_writable,
            )
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing to read through symlink")
    fd = os.open(path, flags)
    return _read_open_bytes_fd(
        fd,
        max_bytes=max_bytes,
        require_private=require_private,
        require_owner=require_owner,
        reject_group_other_writable=reject_group_other_writable,
    )


def read_root_confined_text(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int = 1_000_000,
    require_private: bool = True,
    require_owner: bool = False,
    reject_group_other_writable: bool = False,
) -> tuple[str, os.stat_result]:
    """Read bounded UTF-8 text from a regular file without following links."""
    data, state = read_root_confined_bytes(
        path,
        root=root,
        max_bytes=max_bytes,
        require_private=require_private,
        require_owner=require_owner,
        reject_group_other_writable=reject_group_other_writable,
    )
    return data.decode("utf-8"), state


def read_root_confined_tail_bytes(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int = 1_000_000,
    require_private: bool = True,
    require_owner: bool = False,
    reject_group_other_writable: bool = False,
) -> tuple[bytes, os.stat_result, bool]:
    """Read at most the final ``max_bytes`` from a confined regular file.

    The final boolean is true when the returned bytes contain the complete
    file rather than a suffix. The descriptor is validated before seeking, so
    callers can safely inspect large append-only ledgers without following
    links or allocating proportional to total file size.
    """
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_context = (
        _open_confined_parent_fd(path, root=Path(root), create=False)
        if root is not None and os.name != "nt"
        else None
    )
    if parent_context is not None:
        with parent_context as parent_fd:
            try:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            return _read_open_tail_bytes_fd(
                fd,
                max_bytes=max_bytes,
                require_private=require_private,
                require_owner=require_owner,
                reject_group_other_writable=reject_group_other_writable,
            )
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing to read through symlink")
    fd = os.open(path, flags)
    return _read_open_tail_bytes_fd(
        fd,
        max_bytes=max_bytes,
        require_private=require_private,
        require_owner=require_owner,
        reject_group_other_writable=reject_group_other_writable,
    )


def iter_root_confined_text_lines(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int = 100_000_000,
    max_line_bytes: int = 1_000_000,
    require_private: bool = True,
    require_owner: bool = False,
    reject_group_other_writable: bool = False,
) -> Iterator[str]:
    """Stream UTF-8 lines from a trusted confined file with byte and line caps."""
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_context = (
        _open_confined_parent_fd(path, root=Path(root), create=False)
        if root is not None and os.name != "nt"
        else None
    )
    fd = -1
    if parent_context is not None:
        with parent_context as parent_fd:
            try:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            yield from _iter_open_text_lines_fd(
                fd,
                max_bytes=max_bytes,
                max_line_bytes=max_line_bytes,
                require_private=require_private,
                require_owner=require_owner,
                reject_group_other_writable=reject_group_other_writable,
            )
        return
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing to read through symlink")
    fd = os.open(path, flags)
    yield from _iter_open_text_lines_fd(
        fd,
        max_bytes=max_bytes,
        max_line_bytes=max_line_bytes,
        require_private=require_private,
        require_owner=require_owner,
        reject_group_other_writable=reject_group_other_writable,
    )


def _iter_open_text_lines_fd(
    fd: int,
    *,
    max_bytes: int,
    max_line_bytes: int,
    require_private: bool,
    require_owner: bool,
    reject_group_other_writable: bool,
) -> Iterator[str]:
    try:
        state = os.fstat(fd)
        _validate_open_read_state(
            state,
            require_private=require_private,
            require_owner=require_owner,
            reject_group_other_writable=reject_group_other_writable,
        )
        byte_cap = max(0, int(max_bytes))
        line_cap = max(1, int(max_line_bytes))
        if state.st_size > byte_cap:
            raise OSError("private read target exceeds size limit")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            total = 0
            while True:
                raw = handle.readline(line_cap + 1)
                if not raw:
                    break
                total += len(raw)
                if total > byte_cap:
                    raise OSError("private read target exceeds size limit")
                if len(raw) > line_cap:
                    raise OSError("private read line exceeds size limit")
                yield raw.decode("utf-8")
    finally:
        if fd >= 0:
            os.close(fd)


def _validate_open_read_state(
    state: os.stat_result,
    *,
    require_private: bool,
    require_owner: bool,
    reject_group_other_writable: bool,
) -> None:
    if not stat.S_ISREG(state.st_mode):
        raise OSError("private read target is not a regular file")
    _require_single_link(state)
    if os.name == "nt":
        return
    mode = stat.S_IMODE(state.st_mode)
    if require_private and mode & 0o077:
        raise PermissionError("private read target has group/other permissions")
    if reject_group_other_writable and mode & 0o022:
        raise PermissionError("read target is group/other writable")
    if (require_private or require_owner) and hasattr(os, "geteuid") and state.st_uid != os.geteuid():
        raise PermissionError("private read target owner mismatch")


def _read_open_bytes_fd(
    fd: int,
    *,
    max_bytes: int,
    require_private: bool,
    require_owner: bool,
    reject_group_other_writable: bool,
) -> tuple[bytes, os.stat_result]:
    try:
        state = os.fstat(fd)
        _validate_open_read_state(
            state,
            require_private=require_private,
            require_owner=require_owner,
            reject_group_other_writable=reject_group_other_writable,
        )
        limit = max(0, int(max_bytes))
        if state.st_size > limit:
            raise OSError(f"private read exceeds {limit} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OSError(f"private read exceeds {limit} bytes")
        return b"".join(chunks), state
    finally:
        os.close(fd)


def _read_open_tail_bytes_fd(
    fd: int,
    *,
    max_bytes: int,
    require_private: bool,
    require_owner: bool,
    reject_group_other_writable: bool,
) -> tuple[bytes, os.stat_result, bool]:
    try:
        state = os.fstat(fd)
        _validate_open_read_state(
            state,
            require_private=require_private,
            require_owner=require_owner,
            reject_group_other_writable=reject_group_other_writable,
        )
        limit = max(0, int(max_bytes))
        start = max(0, state.st_size - limit)
        os.lseek(fd, start, os.SEEK_SET)
        remaining = state.st_size - start
        chunks: list[bytes] = []
        total = 0
        while total < remaining:
            chunk = os.read(fd, min(65536, remaining - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), state, start == 0
    finally:
        os.close(fd)


@contextmanager
def private_file_lock(path: Path, *, root: Path | None = None) -> Iterator[None]:
    """Hold a cross-platform exclusive lock on a private, confined lock file."""
    path = Path(path)
    if root is not None and os.name != "nt":
        with _open_confined_parent_fd(path, root=Path(root), create=True) as parent_fd:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = _open_or_create_private_file(parent_fd, path.name, flags)
            except OSError as exc:
                _raise_confined_path_error(exc)
            handle = os.fdopen(fd, "r+b", buffering=0)
            try:
                _require_private_mutation_target(os.fstat(handle.fileno()))
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                handle.seek(0)
                _lock_handle_required(handle)
                yield
            finally:
                _unlock_handle_required(handle)
                handle.close()
        return
    _require_root_confined_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("refusing to lock through symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    try:
        _require_private_mutation_target(os.fstat(handle.fileno()))
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        _lock_handle_required(handle)
        yield
    finally:
        _unlock_handle_required(handle)
        handle.close()
    if os.name != "nt":
        path.chmod(0o600)


@contextmanager
def private_file_try_lock(
    path: Path,
    *,
    root: Path | None = None,
) -> Iterator[bool]:
    """Try to hold a private exclusive lock without blocking.

    The lock file is opened with the same confinement, no-follow, ownership,
    and single-link checks as :func:`private_file_lock`. The context yields
    ``False`` when another process owns the lock or non-blocking locking is not
    available.
    """
    path = Path(path)
    if root is not None and os.name != "nt":
        with _open_confined_parent_fd(path, root=Path(root), create=True) as parent_fd:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = _open_or_create_private_file(parent_fd, path.name, flags)
            except OSError as exc:
                _raise_confined_path_error(exc)
            handle = os.fdopen(fd, "r+b", buffering=0)
            acquired = False
            try:
                _require_private_mutation_target(os.fstat(handle.fileno()))
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                handle.seek(0)
                acquired = _try_lock_handle(handle)
                yield acquired
            finally:
                if acquired:
                    _unlock_handle_required(handle)
                handle.close()
        return
    _require_root_confined_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("refusing to lock through symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    acquired = False
    try:
        _require_private_mutation_target(os.fstat(handle.fileno()))
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        acquired = _try_lock_handle(handle)
        yield acquired
    finally:
        if acquired:
            _unlock_handle_required(handle)
        handle.close()
    if os.name != "nt":
        path.chmod(0o600)


def claim_private_ttl_marker(
    path: Path,
    *,
    root: Path,
    ttl_seconds: float,
    text: str = "running",
    now: float | None = None,
    max_future_skew_seconds: float = 300.0,
) -> bool:
    """Atomically claim a private marker when no trusted recent claim exists.

    A sibling guard serializes contenders. Untrusted marker state (symlink,
    hardlink, public mode, wrong owner, malformed content, or an mtime too far
    in the future) is never followed; it is replaced atomically inside the
    confined parent. Small future skew is tolerated for clock synchronization.
    """
    path = Path(path)
    root = Path(root)
    guard = path.with_name(f".{path.name}.claim-lock")
    current_time = time.time() if now is None else float(now)
    ttl = max(0.0, float(ttl_seconds))
    future_skew = min(ttl, max(0.0, float(max_future_skew_seconds)))
    with private_file_lock(guard, root=root):
        try:
            _content, state = read_root_confined_text(
                path,
                root=root,
                max_bytes=1024,
                require_private=True,
            )
        except (OSError, UnicodeDecodeError):
            state = None
        if state is not None and ttl > 0.0:
            age = current_time - float(state.st_mtime)
            if -future_skew <= age < ttl:
                return False
        atomic_write_private_text(path, text, root=root)
        return True


def release_private_ttl_marker(
    path: Path,
    *,
    root: Path,
    expected_text: str,
) -> bool:
    """Remove a private marker only while it is still owned by *expected_text*."""
    path = Path(path)
    root = Path(root)
    guard = path.with_name(f".{path.name}.claim-lock")
    with private_file_lock(guard, root=root):
        try:
            content, state = read_root_confined_text(
                path,
                root=root,
                max_bytes=1024,
                require_private=True,
            )
        except (OSError, UnicodeDecodeError):
            return False
        if content != str(expected_text):
            return False
        if os.name != "nt":
            try:
                with _open_confined_parent_fd(path, root=root, create=False) as parent_fd:
                    current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISREG(current.st_mode):
                        return False
                    _require_single_link(current)
                    if (current.st_dev, current.st_ino) != (state.st_dev, state.st_ino):
                        return False
                    os.unlink(path.name, dir_fd=parent_fd)
                    try:
                        os.fsync(parent_fd)
                    except OSError:
                        pass
                    return True
            except (FileNotFoundError, OSError):
                return False
        _require_root_confined_parent(path, root)
        try:
            current = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
            return False
        _require_single_link(current)
        if (current.st_dev, current.st_ino) != (state.st_dev, state.st_ino):
            return False
        path.unlink()
        return True


def _lock_handle_required(handle) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return
        except (ImportError, OSError) as exc:
            raise OSError("exclusive file locking unavailable") from exc
    if fcntl is None:
        raise OSError("exclusive file locking unavailable")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise OSError("exclusive file locking unavailable") from exc


def _try_lock_handle(handle) -> bool:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (ImportError, OSError):
            return False
    if fcntl is None:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _unlock_handle_required(handle) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
        return
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
