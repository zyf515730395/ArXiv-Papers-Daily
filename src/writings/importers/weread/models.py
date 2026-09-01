"""Immutable private contracts for WeChat Reading inspection."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping


@dataclass(frozen=True, slots=True)
class NoteSection:
    """One recognized private note section, preserving chapter/item order."""

    name: Literal["highlights", "thoughts", "reviews"]
    items: tuple[tuple[str | None, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple((chapter, text) for chapter, text in self.items))


@dataclass(frozen=True, slots=True)
class BookNotes:
    """Normalized private source material; never serialize this object publicly."""

    source_ref: str
    source_fingerprint: str
    title: str
    author: str | None
    book_id: str | None
    category: str | None
    updated_at: str | None
    sections: tuple[NoteSection, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class WeReadArticlePlan:
    """Editable public metadata proposal with fixed article semantics."""

    source_ref: str
    detected_title: str
    detected_author: str | None
    include: bool
    slug: str
    title: str
    published_at: str | None
    summary: str | None
    tags: tuple[str, ...]
    kind: Literal["book-note"] = field(default="book-note", init=False)
    source: Literal["wechat-reading"] = field(default="wechat-reading", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))


@dataclass(frozen=True, slots=True)
class WeReadPlan:
    """Versioned deterministic plan for a local WeChat Reading export."""

    version: Literal[1]
    source: Literal["wechat-reading-export"]
    export_fingerprint: str
    books: tuple[WeReadArticlePlan, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "books", tuple(self.books))
