"""Testable durability primitives for importer transaction boundaries."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
from typing import BinaryIO, Callable


_BOUNDARY_HOOK: Callable[[str], None] = lambda label: None
_UNSUPPORTED_DIRECTORY_ERRNOS = {
    errno.EACCES,
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def _boundary(label: str) -> None:
    _BOUNDARY_HOOK(label)


def _flush_handle(handle: BinaryIO, label: str) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    _boundary(f"{label}:file")


def _flush_windows_directory(directory: Path) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        flush = kernel32.FlushFileBuffers
        flush.argtypes = (wintypes.HANDLE,)
        flush.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        handle = create_file(
            str(directory),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0x1 | 0x2 | 0x4,  # read/write/delete sharing
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle == invalid:
            return False
        try:
            return bool(flush(handle))
        finally:
            close(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def flush_directory(directory: str | Path, label: str) -> bool:
    """Flush directory metadata when the platform exposes a provable primitive."""
    target = Path(directory)
    supported = False
    if os.name == "nt":
        supported = _flush_windows_directory(target)
    else:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            os.fsync(descriptor)
            supported = True
        except OSError as error:
            if error.errno not in _UNSUPPORTED_DIRECTORY_ERRNOS:
                raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
    _boundary(label)
    return supported


def durable_mkdirs(path: str | Path, label: str) -> bool:
    """Create missing ancestors and flush every new directory entry."""
    target = Path(path)
    missing: list[Path] = []
    current = target
    while not os.path.lexists(current):
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise OSError("directory has no existing ancestor")
        current = parent
    if current.is_symlink() or not current.is_dir():
        raise OSError("directory ancestor is unsafe")
    supported = True
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise OSError("directory creation raced with unsafe entry")
        _boundary(f"{label}:mkdir")
        supported = flush_directory(
            directory.parent, f"{label}:parent"
        ) and supported
    if missing:
        supported = flush_directory(target, f"{label}:directory") and supported
    return supported


def _move_file_ex(source: Path, target: Path, *, replace: bool) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    flags = 0x8 | (0x1 if replace else 0)  # WRITE_THROUGH, optionally REPLACE
    if not move(str(source), str(target), flags):
        code = ctypes.get_last_error()
        if not replace and code in {80, 183}:  # FILE_EXISTS / ALREADY_EXISTS
            raise FileExistsError(code, "rename target already exists", str(target))
        raise ctypes.WinError(code)


def _replace_path(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return
    _move_file_ex(source, target, replace=True)


def _rename_noreplace_posix(source: Path, target: Path) -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = (
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            rename.restype = ctypes.c_int
            arguments = (
                os.fsencode(source),
                os.fsencode(target),
                0x00000004,  # RENAME_EXCL
            )
        else:
            rename = libc.renameat2
            rename.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            rename.restype = ctypes.c_int
            arguments = (
                -100,
                os.fsencode(source),
                -100,
                os.fsencode(target),
                1,  # RENAME_NOREPLACE
            )
    except (AttributeError, OSError) as error:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable") from error
    result = rename(*arguments)
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, "rename target already exists", str(target))
    raise OSError(code, os.strerror(code), str(target))


def _rename_noreplace(source: Path, target: Path) -> None:
    if os.name == "nt":
        _move_file_ex(source, target, replace=False)
        return
    _rename_noreplace_posix(source, target)


def durable_replace_path(
    source: str | Path,
    target: str | Path,
    label: str,
) -> bool:
    """Atomically rename and then durably flush the containing directory."""
    source_path = Path(source)
    target_path = Path(target)
    if source_path.parent != target_path.parent:
        raise OSError("durable replace requires sibling paths")
    _replace_path(source_path, target_path)
    _boundary(label)
    return flush_directory(target_path.parent, f"{label}:parent")


def durable_rename_noreplace(
    source: str | Path,
    target: str | Path,
    label: str,
) -> bool:
    """Atomically rename a sibling tree without replacing a raced target."""
    source_path = Path(source)
    target_path = Path(target)
    if source_path.parent != target_path.parent:
        raise OSError("durable rename requires sibling paths")
    _rename_noreplace(source_path, target_path)
    _boundary(label)
    return flush_directory(target_path.parent, f"{label}:parent")


def durable_rename_noreplace_across_parents(
    source: str | Path,
    target: str | Path,
    label: str,
) -> bool:
    """Atomically move without replacement and flush both directory entries."""
    source_path = Path(source)
    target_path = Path(target)
    if source_path.parent == target_path.parent:
        return durable_rename_noreplace(source_path, target_path, label)
    _rename_noreplace(source_path, target_path)
    _boundary(label)
    source_supported = flush_directory(
        source_path.parent, f"{label}:source-parent"
    )
    target_supported = flush_directory(
        target_path.parent, f"{label}:target-parent"
    )
    return source_supported and target_supported


def durable_atomic_write(
    path: str | Path,
    data: bytes,
    label: str,
    transaction_id: str | None = None,
) -> bool:
    """Write bytes through a flushed sibling and durably replace the target."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = transaction_id or str(os.getpid())
    temporary = target.with_name(
        f".{target.name}.{identity}-{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            _flush_handle(handle, label)
        _replace_path(temporary, target)
        _boundary(f"{label}:replace")
        return flush_directory(target.parent, f"{label}:parent")
    finally:
        if os.path.lexists(temporary):
            temporary.unlink(missing_ok=True)


def durable_append(path: str | Path, data: bytes, label: str) -> bool:
    """Append one checksummed journal record and flush it before returning."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = os.path.lexists(target)
    with target.open("ab", buffering=0) as handle:
        remaining = memoryview(data)
        while remaining:
            written = handle.write(remaining)
            if written is None or written <= 0:
                raise OSError("journal append made no progress")
            remaining = remaining[written:]
        os.fsync(handle.fileno())
    _boundary(label)
    if not existed:
        return flush_directory(target.parent, f"{label}:parent")
    return True


def durable_unlink(path: str | Path, label: str) -> bool:
    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    _boundary(label)
    return flush_directory(target.parent, f"{label}:parent")


def durable_remove_tree(path: str | Path, label: str) -> bool:
    target = Path(path)
    shutil.rmtree(target)
    _boundary(label)
    return flush_directory(target.parent, f"{label}:parent")


def make_tree_durable(root: str | Path, label: str = "stage") -> bool:
    """Flush regular files and then directories deepest-first without recursion."""
    tree = Path(root)
    pending = [tree]
    directories: list[Path] = []
    files: list[Path] = []
    while pending:
        current = pending.pop()
        directories.append(current)
        children = sorted(current.iterdir(), key=lambda child: child.name)
        nested: list[Path] = []
        for child in children:
            details = child.lstat()
            if child.is_symlink():
                raise OSError("durable tree contains a link")
            if stat.S_ISDIR(details.st_mode):
                nested.append(child)
            elif stat.S_ISREG(details.st_mode):
                files.append(child)
            else:
                raise OSError("durable tree contains a special file")
        pending.extend(reversed(nested))
    for path in sorted(files, key=lambda item: item.as_posix()):
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
        _boundary(f"{label}:file")
    supported = True
    for directory in sorted(
        directories, key=lambda item: (-len(item.parts), item.as_posix())
    ):
        supported = flush_directory(directory, f"{label}:directory") and supported
    return supported
