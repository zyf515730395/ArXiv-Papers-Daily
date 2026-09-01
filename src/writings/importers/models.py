"""Immutable contracts for private, offline writing imports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Literal, Mapping


_NOTION_ID = re.compile(r"(?i)\b[0-9a-f]{32}\b")


def _safe_error_message(message: str) -> str:
    """Keep user-facing importer failures free from private identifiers."""
    value = _NOTION_ID.sub("[notion-id]", str(message))
    value = re.sub(r"(?i)(?:[a-z]:[\\/]|//|/)[^\s:;]+", "[path]", value)
    return " ".join(value.split()) or "import failed"


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
class ImportIssue:
    source: str
    code: str
    message: str


class NotionImportError(ValueError):
    """A stable, privacy-safe import error suitable for the command line."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = _safe_error_message(message)
        super().__init__(f"{self.code}: {self.message}")
