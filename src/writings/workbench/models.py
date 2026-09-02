"""Immutable, redacted contracts for the local knowledge workbench."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SourceName = Literal["original", "notion", "weread"]


class WorkbenchError(ValueError):
    """A stable local-workflow failure safe to show without a traceback."""

    def __init__(self, code: str, message: str) -> None:
        safe_code = code if code.replace("_", "").isalnum() else "workbench_failed"
        safe_message = " ".join(str(message).split())[:300] or "local workflow failed safely"
        super().__init__(safe_message)
        self.code = safe_code
        self.message = safe_message


@dataclass(frozen=True, slots=True)
class WorkbenchIssue:
    code: str
    message: str

    def __post_init__(self) -> None:
        safe = WorkbenchError(self.code, self.message)
        object.__setattr__(self, "code", safe.code)
        object.__setattr__(self, "message", safe.message)


@dataclass(frozen=True, slots=True)
class DraftRecord:
    slug: str
    status: Literal["draft", "ready", "unchanged", "conflict", "attention"]
    issue: WorkbenchIssue | None = None


@dataclass(frozen=True, slots=True)
class SourceSummary:
    source: SourceName
    status: Literal["not-started", "ready", "degraded", "attention"]
    total: int = 0
    actionable: int = 0


@dataclass(frozen=True, slots=True)
class BuildSummary:
    status: Literal["not-started", "success", "degraded", "failed"]
    published: int = 0
    retained: int = 0
    skipped: int = 0
    removed: int = 0


@dataclass(frozen=True, slots=True)
class WorkbenchStatus:
    drafts: tuple[DraftRecord, ...]
    sources: tuple[SourceSummary, ...]
    build: BuildSummary


@dataclass(frozen=True, slots=True)
class OriginalResult:
    slug: str
    status: Literal["ready", "applied", "unchanged", "conflict", "blocked"]
    issue: WorkbenchIssue | None = None
