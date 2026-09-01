"""Safe local inventory for Notion Markdown and CSV exports."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import tempfile
from typing import ContextManager, Iterator
import zipfile

from .models import ExportFile, ExportInventory, ImportLimits, NotionImportError


DEFAULT_LIMITS = ImportLimits()
_REPARSE_POINT = 0x0400


def _fail(message: str) -> NotionImportError:
    return NotionImportError("unsafe_archive", message)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as error:
        raise _fail("unable to inspect archive entry") from error
    is_junction = getattr(path, "is_junction", None)
    try:
        junction = bool(is_junction and is_junction())
    except OSError:
        junction = True
    attributes = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or junction or bool(attributes & _REPARSE_POINT)


def _validated_member(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name:
        raise _fail("archive member path is unsafe")
    windows = PureWindowsPath(name)
    posix = PurePosixPath(name)
    lowered = name.lower()
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or lowered.startswith(("//?", "//./"))
        or posix == PurePosixPath(".")
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.as_posix() != name
    ):
        raise _fail("archive member path is unsafe")
    return posix


def _validate_limits(count: int, size: int, total: int, limits: ImportLimits) -> int:
    if count > limits.max_members or size > limits.max_file_bytes or total + size > limits.max_total_bytes:
        raise _fail("archive exceeds configured safety limits")
    return total + size


def _fingerprint(files: dict[str, ExportFile]) -> str:
    digest = hashlib.sha256()
    for name, record in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _hash_file(source: Path, limits: ImportLimits) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > limits.max_file_bytes:
                    raise _fail("archive exceeds configured safety limits")
                digest.update(chunk)
    except OSError as error:
        raise _fail("unable to read archive entry") from error
    return size, digest.hexdigest()


def _inventory(root: Path, entries: list[tuple[PurePosixPath, Path]], limits: ImportLimits) -> ExportInventory:
    files: dict[str, ExportFile] = {}
    total = 0
    for relative, source in sorted(entries, key=lambda item: item[0].as_posix()):
        name = relative.as_posix()
        size, digest = _hash_file(source, limits)
        total = _validate_limits(len(files) + 1, size, total, limits)
        files[name] = ExportFile(relative, source, size, digest)
    names = tuple(sorted(files))
    return ExportInventory(
        root=root,
        files=files,
        markdown_paths=tuple(name for name in names if name.lower().endswith(".md")),
        csv_paths=tuple(name for name in names if name.lower().endswith(".csv")),
        fingerprint=_fingerprint(files),
    )


def _work_root(value: str | Path) -> Path:
    root = Path(value).resolve()
    if root.name != "notion-import" or root.parent.name != "build":
        raise _fail("temporary extraction root must be build/notion-import")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _directory_inventory(source: Path, limits: ImportLimits) -> ExportInventory:
    if _is_link_or_reparse(source) or not source.is_dir():
        raise _fail("export directory is unsafe")
    entries: list[tuple[PurePosixPath, Path]] = []
    seen: set[str] = set()
    total = 0
    count = 0

    def walk(current: Path, prefix: PurePosixPath | None = None) -> None:
        nonlocal count, total
        try:
            children = sorted(current.iterdir(), key=lambda child: child.name.casefold())
        except OSError as error:
            raise _fail("unable to read export directory") from error
        for child in children:
            count += 1
            if count > limits.max_members:
                raise _fail("archive exceeds configured safety limits")
            if _is_link_or_reparse(child):
                raise _fail("export directory contains a link or reparse point")
            relative = _validated_member((prefix / child.name).as_posix() if prefix else child.name)
            if child.is_dir():
                walk(child, relative)
                continue
            try:
                details = child.lstat()
            except OSError as error:
                raise _fail("unable to inspect export entry") from error
            if not stat.S_ISREG(details.st_mode):
                raise _fail("export directory contains a special file")
            total = _validate_limits(count, details.st_size, total, limits)
            key = relative.as_posix().casefold()
            if key in seen:
                raise _fail("export contains ambiguous member paths")
            seen.add(key)
            entries.append((relative, child))

    walk(source)
    return _inventory(source.resolve(), entries, limits)


def _zip_entries(archive: zipfile.ZipFile, limits: ImportLimits) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    records: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[str] = set()
    total = 0
    if len(archive.infolist()) > limits.max_members:
        raise _fail("archive exceeds configured safety limits")
    for info in archive.infolist():
        raw_name = info.filename[:-1] if info.is_dir() and info.filename.endswith("/") else info.filename
        relative = _validated_member(raw_name)
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if info.flag_bits & 0x1 or (file_type and file_type != stat.S_IFREG):
            raise _fail("archive contains an unsupported member type")
        key = relative.as_posix().casefold()
        if key in seen:
            raise _fail("archive contains ambiguous member paths")
        seen.add(key)
        if info.is_dir():
            continue
        total = _validate_limits(len(records) + 1, info.file_size, total, limits)
        records.append((info, relative))
    return records


def _validate_raw_zip_names(source: Path, archive: zipfile.ZipFile) -> None:
    """Validate original central-directory names before zipfile normalizes separators."""
    try:
        payload = source.read_bytes()
        offset = archive.start_dir
    except (AttributeError, OSError) as error:
        raise _fail("unable to inspect ZIP member names") from error
    found = 0
    while payload[offset : offset + 4] == b"PK\x01\x02":
        if len(payload) < offset + 46:
            raise _fail("ZIP central directory is truncated")
        flags = int.from_bytes(payload[offset + 8 : offset + 10], "little")
        name_size = int.from_bytes(payload[offset + 28 : offset + 30], "little")
        extra_size = int.from_bytes(payload[offset + 30 : offset + 32], "little")
        comment_size = int.from_bytes(payload[offset + 32 : offset + 34], "little")
        end = offset + 46 + name_size + extra_size + comment_size
        if end > len(payload):
            raise _fail("ZIP central directory is truncated")
        encoding = "utf-8" if flags & 0x800 else "cp437"
        try:
            raw_name = payload[offset + 46 : offset + 46 + name_size].decode(encoding)
        except UnicodeDecodeError as error:
            raise _fail("ZIP member name is not decodable") from error
        _validated_member(raw_name[:-1] if raw_name.endswith("/") else raw_name)
        found += 1
        offset = end
    if found != len(archive.infolist()):
        raise _fail("ZIP central directory is malformed")


def _extract_zip(source: Path, work_root: Path, limits: ImportLimits) -> tuple[Path, ExportInventory]:
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise _fail("input is not a readable ZIP export") from error
    with archive:
        _validate_raw_zip_names(source, archive)
        records = _zip_entries(archive, limits)
        runs = work_root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        extraction = Path(tempfile.mkdtemp(prefix="run-", dir=runs)).resolve()
        if not extraction.is_relative_to(runs.resolve()):
            raise _fail("temporary extraction path is unsafe")
        entries: list[tuple[PurePosixPath, Path]] = []
        try:
            for info, relative in records:
                target = extraction.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                digest = hashlib.sha256()
                with archive.open(info, "r") as reader, target.open("xb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        written += len(chunk)
                        if written > limits.max_file_bytes:
                            raise _fail("archive exceeds configured safety limits")
                        digest.update(chunk)
                        writer.write(chunk)
                if written != info.file_size:
                    raise _fail("archive member size changed while reading")
                entries.append((relative, target))
            return extraction, _inventory(extraction, entries, limits)
        except Exception:
            if extraction.is_relative_to(runs.resolve()):
                shutil.rmtree(extraction, ignore_errors=True)
            raise


@contextmanager
def open_export(
    source: str | Path,
    work_root: str | Path,
    limits: ImportLimits = DEFAULT_LIMITS,
) -> Iterator[ExportInventory]:
    """Safely inventory a directory or ZIP, cleaning up only owned extraction runs."""
    candidate = Path(source)
    if _is_link_or_reparse(candidate):
        raise _fail("export input must not be a link or reparse point")
    if candidate.is_dir():
        yield _directory_inventory(candidate, limits)
        return
    if not candidate.is_file() or candidate.suffix.lower() != ".zip":
        raise _fail("export input must be a ZIP file or directory")
    root = _work_root(work_root)
    extraction, inventory = _extract_zip(candidate, root, limits)
    try:
        yield inventory
    finally:
        runs = (root / "runs").resolve()
        if extraction.is_relative_to(runs):
            shutil.rmtree(extraction, ignore_errors=True)
