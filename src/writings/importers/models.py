"""Immutable contracts for private, offline writing imports."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Callable, Literal, Mapping
import unicodedata


_NOTION_ID = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|"
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?![0-9a-f])"
)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", "clock$"} | {f"com{number}" for number in range(1, 10)} | {f"lpt{number}" for number in range(1, 10)}
_REPARSE_POINT = 0x0400
_WEREAD_BOOK_ID = re.compile(
    r"(?i)\b(?:book(?:[_ -]?id)?\s*[:=]?\s*)?[0-9]{6,}\b"
)
_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\|//|/)[^:;,\r\n]*"
)
_SOURCE_FILENAME = re.compile(
    r"(?i)(?<!\S)[^\\/:;,\r\n]*?\.md(?=$|[\s\x00-\x1f:;,])"
)


def _safe_error_message(message: str) -> str:
    """Keep user-facing importer failures free from private identifiers."""
    value = _NOTION_ID.sub("[notion-id]", str(message))
    value = re.sub(r"(?i)(?:[a-z]:[\\/]|//|/)[^\s:;]+", "[path]", value)
    return " ".join(value.split()) or "import failed"


def _safe_weread_error_message(message: str) -> str:
    """Keep WeRead failures free from book and local source identities."""
    value = _ABSOLUTE_PATH.sub("[path]", str(message))
    value = _WEREAD_BOOK_ID.sub("[book-id]", value)
    value = _SOURCE_FILENAME.sub("[source-file]", value)
    value = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
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


class WritingImportError(ValueError):
    """A stable importer error that never retains its unsanitized message."""

    def __init__(
        self,
        code: str,
        message: str,
        sanitizer: Callable[[str], str] = _safe_error_message,
    ) -> None:
        self.code = str(code)
        self.message = str(sanitizer(str(message))) or "import failed"
        super().__init__(f"{self.code}: {self.message}")


class NotionImportError(WritingImportError):
    """A stable, privacy-safe Notion import error for the command line."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, _safe_error_message)


class WeReadImportError(WritingImportError):
    """A stable, privacy-safe WeRead import error for the command line."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, _safe_weread_error_message)


@dataclass(frozen=True, slots=True)
class ImportNamespace:
    name: Literal["notion-import", "weread-import", "writings-workbench"]
    report_name: Literal[
        "notion-import.json", "weread-import.json", "writings-workbench.json"
    ]

    def __post_init__(self) -> None:
        if (self.name, self.report_name) not in {
            ("notion-import", "notion-import.json"),
            ("weread-import", "weread-import.json"),
            ("writings-workbench", "writings-workbench.json"),
        }:
            raise ValueError("unsupported import namespace")


NOTION_NAMESPACE = ImportNamespace("notion-import", "notion-import.json")
WEREAD_NAMESPACE = ImportNamespace("weread-import", "weread-import.json")
WORKBENCH_NAMESPACE = ImportNamespace(
    "writings-workbench", "writings-workbench.json"
)


def _namespace_error(
    namespace: ImportNamespace, code: str, message: str
) -> WritingImportError:
    error_type = NotionImportError if namespace == NOTION_NAMESPACE else WeReadImportError
    return error_type(code, message)


def _private_root_components(
    project: Path, namespace: ImportNamespace
) -> tuple[Path, ...]:
    build = project / "build"
    root = build / namespace.name
    return (*project.parents[::-1], project, build, root)


def canonical_private_root(namespace: ImportNamespace) -> Path:
    project = Path(PROJECT_ROOT)
    build = project / "build"
    root = build / namespace.name
    for component in _private_root_components(project, namespace):
        if os.path.lexists(component) and _is_link_or_reparse(component):
            raise _namespace_error(
                namespace,
                "unsafe_archive",
                "private import root contains a link or reparse point",
            )
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _namespace_error(
            namespace, "unsafe_archive", "unable to create private import root"
        ) from error
    resolved_project = project.resolve()
    resolved_root = root.resolve()
    if not resolved_root.is_relative_to(resolved_project):
        raise _namespace_error(
            namespace, "unsafe_archive", "private import root escapes the project"
        )
    return resolved_root


def canonical_import_root() -> Path:
    return canonical_private_root(NOTION_NAMESPACE)


def _validated_private_path(
    value: str | Path,
    namespace: ImportNamespace,
    *,
    exact_root: bool = False,
) -> Path:
    root = canonical_private_root(namespace)
    raw = Path(value)
    lexical = Path(os.path.abspath(raw))
    try:
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise _namespace_error(
            namespace,
            "unsafe_archive",
            "private import path is outside the canonical root",
        ) from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise _namespace_error(
            namespace, "unsafe_archive", "private import path is unsafe"
        )
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current) and _is_link_or_reparse(current):
            raise _namespace_error(
                namespace,
                "unsafe_archive",
                "private import path contains a link or reparse point",
            )
    resolved = lexical.resolve()
    if not resolved.is_relative_to(root) or (exact_root and resolved != root):
        raise _namespace_error(
            namespace,
            "unsafe_archive",
            "private import path is outside the canonical root",
        )
    return resolved


def private_import_path(
    value: str | Path,
    namespace: ImportNamespace = NOTION_NAMESPACE,
    *,
    exact_root: bool = False,
) -> Path:
    return _validated_private_path(value, namespace, exact_root=exact_root)


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
    portable_files: Mapping[str, str | None] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        exact = dict(self.files)
        portable: dict[str, str | None] = {}
        for name in exact:
            key = portable_collision_key(name)
            if key in portable and portable[key] != name:
                portable[key] = None
            else:
                portable[key] = name
        object.__setattr__(self, "files", MappingProxyType(exact))
        object.__setattr__(self, "portable_files", MappingProxyType(portable))
        object.__setattr__(self, "markdown_paths", tuple(self.markdown_paths))
        object.__setattr__(self, "csv_paths", tuple(self.csv_paths))


@dataclass(frozen=True, slots=True)
class SelectedRouteIndex:
    exact: Mapping[str, str]
    portable: Mapping[str, str | None]
    identities: Mapping[str, str | None]
    slugs: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "exact", MappingProxyType(dict(self.exact)))
        object.__setattr__(self, "portable", MappingProxyType(dict(self.portable)))
        object.__setattr__(
            self, "identities", MappingProxyType(dict(self.identities))
        )
        object.__setattr__(self, "slugs", frozenset(self.slugs))


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
    dependencies: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(
            self,
            "dependencies",
            MappingProxyType(
                {slug: frozenset(targets) for slug, targets in self.dependencies.items()}
            ),
        )

    def counts(self) -> Mapping[CandidateStatus, int]:
        values = {
            status: sum(candidate.status == status for candidate in self.candidates)
            for status in CANDIDATE_STATUSES
        }
        return MappingProxyType(values)


def _identity_import_result(result: ImportRunResult) -> ImportRunResult:
    return result


@dataclass(frozen=True, slots=True)
class PreparedApplyContract:
    namespace: ImportNamespace
    export_fingerprint: str
    source_refs: tuple[str, ...]
    prepare: Callable[[Path, Path], ImportRunResult]
    project_result: Callable[[ImportRunResult], ImportRunResult] = (
        _identity_import_result
    )
    serialize_report: Callable[[ImportRunResult], str] | None = None
