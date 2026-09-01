"""Immutable contracts for private, offline writing imports."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Literal, Mapping
import unicodedata


_NOTION_ID = re.compile(r"(?i)\b[0-9a-f]{32}\b")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", "clock$"} | {f"com{number}" for number in range(1, 10)} | {f"lpt{number}" for number in range(1, 10)}
_REPARSE_POINT = 0x0400


def _safe_error_message(message: str) -> str:
    """Keep user-facing importer failures free from private identifiers."""
    value = _NOTION_ID.sub("[notion-id]", str(message))
    value = re.sub(r"(?i)(?:[a-z]:[\\/]|//|/)[^\s:;]+", "[path]", value)
    return " ".join(value.split()) or "import failed"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(junction and junction()) or bool(getattr(details, "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return True


def portable_collision_key(path: PurePosixPath | str) -> str:
    return "/".join(unicodedata.normalize("NFKC", part).casefold().rstrip(". ") for part in PurePosixPath(path).parts)


def validate_portable_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("path must be a non-empty portable POSIX path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or posix == PurePosixPath(".") or posix.as_posix() != value:
        raise ValueError("path must be a normalized relative POSIX path")
    for part in posix.parts:
        normalized = unicodedata.normalize("NFKC", part)
        stem = normalized.split(".", 1)[0].casefold()
        if normalized in {"", ".", ".."} or normalized != normalized.rstrip(". ") or ":" in normalized or any(unicodedata.category(char).startswith("C") for char in normalized) or any(char in '<>"|?*' for char in normalized) or stem in _WINDOWS_DEVICES:
            raise ValueError("path contains a Windows-unsafe component")
    return posix


def canonical_import_root() -> Path:
    project = Path(PROJECT_ROOT)
    build = project / "build"
    root = build / "notion-import"
    for component in (project, build, root):
        if component.exists() and _is_link_or_reparse(component):
            raise NotionImportError("unsafe_archive", "private import root contains a link or reparse point")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise NotionImportError("unsafe_archive", "unable to create private import root") from error
    resolved_project = project.resolve()
    resolved_root = root.resolve()
    if not resolved_root.is_relative_to(resolved_project):
        raise NotionImportError("unsafe_archive", "private import root escapes the project")
    return resolved_root


def private_import_path(value: str | Path, *, exact_root: bool = False) -> Path:
    root = canonical_import_root()
    raw = Path(value)
    lexical = Path(os.path.abspath(raw))
    try:
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise NotionImportError("unsafe_archive", "private import path is outside the canonical root") from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise NotionImportError("unsafe_archive", "private import path is unsafe")
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_link_or_reparse(current):
            raise NotionImportError("unsafe_archive", "private import path contains a link or reparse point")
    resolved = lexical.resolve()
    if not resolved.is_relative_to(root) or (exact_root and resolved != root):
        raise NotionImportError("unsafe_archive", "private import path is outside the canonical root")
    return resolved


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_members: int = 10_000
    max_file_bytes: int = 67_108_864
    max_total_bytes: int = 1_073_741_824


@dataclass(frozen=True, slots=True)
class ExportFile:
    relative_path: PurePosixPath
    source_path: Path
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExportInventory:
    root: Path
    files: Mapping[str, ExportFile]
    markdown_paths: tuple[str, ...]
    csv_paths: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        object.__setattr__(self, "markdown_paths", tuple(self.markdown_paths))
        object.__setattr__(self, "csv_paths", tuple(self.csv_paths))


@dataclass(frozen=True, slots=True)
class ImportArticlePlan:
    source_ref: str
    detected_title: str
    include: bool
    slug: str
    title: str
    published_at: str | None
    kind: str | None
    summary: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportPlan:
    version: int
    source: Literal["notion-export"]
    export_fingerprint: str
    articles: tuple[ImportArticlePlan, ...]


@dataclass(frozen=True, slots=True)
class ImportStateEntry:
    source_key: str
    slug: str
    source_fingerprint: str
    written_fingerprint: str


@dataclass(frozen=True, slots=True)
class ImportState:
    version: int
    sources: Mapping[str, ImportStateEntry]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True, slots=True)
class ImportIssue:
    source: str
    code: str
    message: str


CandidateStatus = Literal[
    "ready", "unchanged", "conflict", "blocked", "ignored", "applied"
]
CANDIDATE_STATUSES: tuple[CandidateStatus, ...] = (
    "ready",
    "unchanged",
    "conflict",
    "blocked",
    "ignored",
    "applied",
)


@dataclass(frozen=True, slots=True)
class ConvertedBundle:
    bundle_root: Path
    issues: tuple[ImportIssue, ...]


@dataclass(frozen=True, slots=True)
class ImportCandidateResult:
    source_ref: str
    slug: str | None
    status: CandidateStatus
    issues: tuple[ImportIssue, ...]
    bundle_root: Path | None
    source_fingerprint: str | None
    written_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class ImportRunResult:
    candidates: tuple[ImportCandidateResult, ...]

    def counts(self) -> Mapping[CandidateStatus, int]:
        values = {
            status: sum(candidate.status == status for candidate in self.candidates)
            for status in CANDIDATE_STATUSES
        }
        return MappingProxyType(values)


class NotionImportError(ValueError):
    """A stable, privacy-safe import error suitable for the command line."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = _safe_error_message(message)
        super().__init__(f"{self.code}: {self.message}")
