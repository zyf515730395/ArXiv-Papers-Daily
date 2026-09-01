"""Safe local inventory for Notion Markdown and CSV exports."""

from __future__ import annotations

from bisect import bisect_left
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import ContextManager, Iterator
import zipfile

from .models import ExportFile, ExportInventory, ImportLimits, NotionImportError, portable_collision_key, private_import_path, validate_portable_relative_path


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
    try:
        return validate_portable_relative_path(name)
    except ValueError as error:
        raise _fail("archive member path is unsafe")


def _validate_limits(count: int, size: int, total: int, limits: ImportLimits) -> int:
    if count > limits.max_members or size > limits.max_file_bytes or total + size > limits.max_total_bytes:
        raise _fail("archive exceeds configured safety limits")
    return total + size


def _has_file_prefix_conflict(file_keys: set[str]) -> bool:
    """Find file/descendant aliases with one logarithmic lookup per file key."""
    keys = sorted(file_keys)
    return any(
        (index := bisect_left(keys, key + "/")) < len(keys) and keys[index].startswith(key + "/")
        for key in keys
    )


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
    return private_import_path(value, exact_root=True)


def _directory_inventory(source: Path, limits: ImportLimits) -> ExportInventory:
    if _is_link_or_reparse(source) or not source.is_dir():
        raise _fail("export directory is unsafe")
    entries: list[tuple[PurePosixPath, Path]] = []
    seen: set[str] = set()
    total = 0
    count = 0

    pending: list[tuple[Path, PurePosixPath | None]] = [(source, None)]
    try:
        while pending:
            current, prefix = pending.pop()
            try:
                children = sorted(
                    current.iterdir(), key=lambda child: child.name.casefold()
                )
            except OSError as error:
                raise _fail("unable to read export directory") from error
            nested: list[tuple[Path, PurePosixPath]] = []
            for child in children:
                count += 1
                if count > limits.max_members:
                    raise _fail("archive exceeds configured safety limits")
                if _is_link_or_reparse(child):
                    raise _fail("export directory contains a link or reparse point")
                relative = _validated_member(
                    (prefix / child.name).as_posix() if prefix else child.name
                )
                if child.is_dir():
                    nested.append((child, relative))
                    continue
                try:
                    details = child.lstat()
                except OSError as error:
                    raise _fail("unable to inspect export entry") from error
                if not stat.S_ISREG(details.st_mode):
                    raise _fail("export directory contains a special file")
                total = _validate_limits(count, details.st_size, total, limits)
                key = portable_collision_key(relative)
                if key in seen:
                    raise _fail("export contains ambiguous member paths")
                seen.add(key)
                entries.append((relative, child))
            pending.extend(reversed(nested))
    except NotionImportError:
        raise
    except (OSError, RuntimeError, RecursionError, TypeError, ValueError) as error:
        raise _fail("unable to inventory export directory") from error
    keys = sorted(seen)
    if any(next_key.startswith(key + "/") for key, next_key in zip(keys, keys[1:])):
        raise _fail("export contains file and descendant path conflict")
    try:
        return _inventory(source.resolve(), entries, limits)
    except NotionImportError:
        raise
    except (OSError, RuntimeError, RecursionError, TypeError, ValueError) as error:
        raise _fail("unable to inventory export directory") from error


def _remove_owned_run(extraction: Path, runs: Path) -> None:
    resolved_runs = runs.resolve()
    resolved = extraction.resolve()
    if not resolved.is_relative_to(resolved_runs) or _is_link_or_reparse(resolved):
        raise _fail("temporary extraction path is unsafe")
    try:
        shutil.rmtree(resolved)
    except OSError as error:
        raise _fail("unable to remove temporary extraction") from error
    if resolved.exists():
        raise _fail("unable to remove temporary extraction")


def _zip_entries(archive: zipfile.ZipFile, limits: ImportLimits) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    records: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[str] = set()
    file_keys: set[str] = set()
    total = 0
    if len(archive.infolist()) > limits.max_members:
        raise _fail("archive exceeds configured safety limits")
    for info in archive.infolist():
        raw_name = info.filename[:-1] if info.is_dir() and info.filename.endswith("/") else info.filename
        relative = _validated_member(raw_name)
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        allowed = {0, stat.S_IFDIR} if info.is_dir() else {0, stat.S_IFREG}
        if info.flag_bits & 0x1 or file_type not in allowed:
            raise _fail("archive contains an unsupported member type")
        key = portable_collision_key(relative)
        if key in seen:
            raise _fail("archive contains ambiguous member paths")
        seen.add(key)
        if info.is_dir():
            continue
        file_keys.add(key)
        total = _validate_limits(len(records) + 1, info.file_size, total, limits)
        records.append((info, relative))
    if _has_file_prefix_conflict(file_keys):
        raise _fail("archive contains file and descendant path conflict")
    return records


def _validate_raw_zip_names(source: Path, archive: zipfile.ZipFile) -> None:
    """Validate original central-directory names before zipfile normalizes separators."""
    try:
        stream = source.open("rb")
        stream.seek(archive.start_dir)
    except (AttributeError, OSError) as error:
        raise _fail("unable to inspect ZIP member names") from error
    with stream:
        for _ in archive.infolist():
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise _fail("ZIP central directory is malformed")
            flags = int.from_bytes(header[8:10], "little")
            name_size = int.from_bytes(header[28:30], "little")
            extra_size = int.from_bytes(header[30:32], "little")
            comment_size = int.from_bytes(header[32:34], "little")
            if name_size > 65535 or extra_size + comment_size > 131070:
                raise _fail("ZIP central directory is malformed")
            name = stream.read(name_size)
            if len(name) != name_size:
                raise _fail("ZIP central directory is truncated")
            stream.seek(extra_size + comment_size, 1)
            try:
                raw_name = name.decode("utf-8" if flags & 0x800 else "cp437")
            except UnicodeDecodeError as error:
                raise _fail("ZIP member name is not decodable") from error
            _validated_member(raw_name[:-1] if raw_name.endswith("/") else raw_name)


def _extract_zip(source: Path, work_root: Path, limits: ImportLimits) -> tuple[Path, ExportInventory]:
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise _fail("input is not a readable ZIP export") from error
    with archive:
        _validate_raw_zip_names(source, archive)
        records = _zip_entries(archive, limits)
        runs = private_import_path(work_root / "runs")
        runs.mkdir(parents=True, exist_ok=True)
        runs = private_import_path(runs)
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
        except NotionImportError:
            _remove_owned_run(extraction, runs)
            raise
        except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
            _remove_owned_run(extraction, runs)
            raise _fail("unable to extract ZIP export") from error


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
        _remove_owned_run(extraction, root / "runs")
