"""Strict private import state and repository bundle fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from writings.catalog import SLUG_PATTERN

from .models import (
    ImportState,
    ImportStateEntry,
    NotionImportError,
    portable_collision_key,
    validate_portable_relative_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_KEY = re.compile(r"^(?:notion:[0-9a-f]{32}|path:[0-9a-f]{64})$")
_FINAL_NOTION_ID = re.compile(r"(?i)(?:^|[ _-])([0-9a-f]{32})$")
_REPARSE_POINT = 0x0400


def _invalid_state(message: str) -> NotionImportError:
    return NotionImportError("invalid_state", message)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(junction and junction()) or bool(
            getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
        )
    except OSError:
        return True


def _private_state_path(value: str | Path) -> Path:
    project = Path(PROJECT_ROOT)
    root = project / "build" / "notion-import"
    raw = Path(value)
    lexical = Path(os.path.abspath(raw))
    try:
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise _invalid_state("state path must be below the private import root") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise _invalid_state("state path is unsafe")
    for component in (project, project / "build", root):
        if os.path.lexists(component) and _is_link_or_reparse(component):
            raise _invalid_state("state path contains a link or reparse point")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _invalid_state("unable to create private state directory") from error
    resolved_root = root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current) and _is_link_or_reparse(current):
            raise _invalid_state("state path contains a link or reparse point")
    try:
        resolved = lexical.resolve()
    except (OSError, RuntimeError) as error:
        raise _invalid_state("state path is unsafe") from error
    if not resolved.is_relative_to(resolved_root):
        raise _invalid_state("state path escapes the private import root")
    return lexical


def _strict_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise _invalid_state("state contains duplicate or invalid fields")
        result[key] = value
    return result


def _validated_state(value: Any) -> ImportState:
    if not isinstance(value, dict) or set(value) != {"version", "sources"}:
        raise _invalid_state("state must contain exactly version and sources")
    if type(value["version"]) is not int or value["version"] != 1:
        raise _invalid_state("state version is unsupported")
    if not isinstance(value["sources"], dict):
        raise _invalid_state("state sources must be an object")
    entries: dict[str, ImportStateEntry] = {}
    owned_slugs: set[str] = set()
    for key in sorted(value["sources"]):
        record = value["sources"][key]
        if not isinstance(key, str) or not _SOURCE_KEY.fullmatch(key):
            raise _invalid_state("state source identity is invalid")
        if not isinstance(record, dict) or set(record) != {
            "slug",
            "source_fingerprint",
            "written_fingerprint",
        }:
            raise _invalid_state("state source records contain unsupported fields")
        slug = record["slug"]
        source_fingerprint = record["source_fingerprint"]
        written_fingerprint = record["written_fingerprint"]
        if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
            raise _invalid_state("state slug is invalid")
        if slug in owned_slugs:
            raise _invalid_state("state assigns one slug to multiple sources")
        if not isinstance(source_fingerprint, str) or not _FINGERPRINT.fullmatch(source_fingerprint):
            raise _invalid_state("state source fingerprint is invalid")
        if not isinstance(written_fingerprint, str) or not _FINGERPRINT.fullmatch(written_fingerprint):
            raise _invalid_state("state bundle fingerprint is invalid")
        owned_slugs.add(slug)
        entries[key] = ImportStateEntry(
            key, slug, source_fingerprint, written_fingerprint
        )
    return ImportState(1, entries)


def load_import_state(path: str | Path) -> ImportState:
    """Load exact-field version-1 private state."""
    target = _private_state_path(path)
    try:
        payload = json.loads(
            target.read_text(encoding="utf-8"), object_pairs_hook=_strict_mapping
        )
    except NotionImportError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise _invalid_state("unable to load private import state") from error
    return _validated_state(payload)


def serialize_import_state(state: ImportState) -> str:
    """Serialize strict state with stable source ordering and no source content."""
    if any(key != entry.source_key for key, entry in state.sources.items()):
        raise _invalid_state("state source identity is inconsistent")
    checked = _validated_state(
        {
            "version": state.version,
            "sources": {
                key: {
                    "slug": entry.slug,
                    "source_fingerprint": entry.source_fingerprint,
                    "written_fingerprint": entry.written_fingerprint,
                }
                for key, entry in state.sources.items()
            },
        }
    )
    payload = {
        "version": checked.version,
        "sources": {
            key: {
                "slug": checked.sources[key].slug,
                "source_fingerprint": checked.sources[key].source_fingerprint,
                "written_fingerprint": checked.sources[key].written_fingerprint,
            }
            for key in sorted(checked.sources)
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_import_state(path: str | Path, state: ImportState) -> None:
    """Atomically replace state through a verified sibling temporary file."""
    target = _private_state_path(path)
    content = serialize_import_state(state).encode("utf-8")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _invalid_state("unable to create private state directory") from error
    temporary = target.with_name(
        f".notion-state-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, target)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _invalid_state("unable to persist private import state") from error


def source_key(source_ref: str) -> str:
    """Return a private stable key without exposing it in diagnostics."""
    try:
        normalized = validate_portable_relative_path(source_ref).as_posix()
    except ValueError as error:
        raise _invalid_state("source identity is invalid") from error
    match = _FINAL_NOTION_ID.search(Path(normalized).stem)
    if match:
        return "notion:" + match.group(1).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return "path:" + digest


def fingerprint_bundle(bundle_root: str | Path) -> str:
    """Hash a regular-file bundle by portable path and bytes."""
    root = Path(bundle_root)
    if not root.exists() or _is_link_or_reparse(root) or not root.is_dir():
        raise _invalid_state("bundle root is unsafe")
    records: list[tuple[str, int, bytes]] = []
    collision_keys: set[str] = set()

    def walk_error(error: OSError) -> None:
        raise error

    try:
        for current, directories, files in os.walk(
            root, topdown=True, onerror=walk_error, followlinks=False
        ):
            current_path = Path(current)
            for name in directories:
                directory = current_path / name
                if _is_link_or_reparse(directory) or not directory.is_dir():
                    raise _invalid_state("bundle contains a link or special file")
                relative = directory.relative_to(root).as_posix()
                normalized = validate_portable_relative_path(relative).as_posix()
                collision = portable_collision_key(normalized)
                if collision in collision_keys:
                    raise _invalid_state("bundle contains ambiguous paths")
                collision_keys.add(collision)
            for name in files:
                path = current_path / name
                details = path.lstat()
                if _is_link_or_reparse(path) or not stat.S_ISREG(details.st_mode):
                    raise _invalid_state("bundle contains a link or special file")
                relative = path.relative_to(root).as_posix()
                normalized = validate_portable_relative_path(relative).as_posix()
                collision = portable_collision_key(normalized)
                if collision in collision_keys:
                    raise _invalid_state("bundle contains ambiguous paths")
                collision_keys.add(collision)
                data = path.read_bytes()
                records.append((normalized, len(data), hashlib.sha256(data).digest()))
    except NotionImportError:
        raise
    except (OSError, ValueError) as error:
        raise _invalid_state("unable to fingerprint bundle") from error
    hasher = hashlib.sha256()
    for relative, size, digest in sorted(records):
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(size).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(digest)
    return "sha256:" + hasher.hexdigest()
