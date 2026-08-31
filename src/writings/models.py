"""Immutable data contracts for the writings publishing domain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping


@dataclass(frozen=True, slots=True)
class WritingArticle:
    slug: str
    title: str
    published_at: date
    kind: Literal["learning-note", "book-note"]
    summary: str
    tags: tuple[str, ...]
    source: Literal["original", "notion", "wechat-reading"]
    source_path: Path
    bundle_root: Path
    body: str


@dataclass(frozen=True, slots=True)
class WritingIssue:
    source: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ManifestArticle:
    source: str
    title: str
    published_at: str
    kind: str
    summary: str
    tags: tuple[str, ...]
    page: str
    assets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WritingManifest:
    version: int
    generated_at: str
    articles: Mapping[str, ManifestArticle]
    managed_files: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "articles", MappingProxyType(dict(self.articles)))
        object.__setattr__(self, "managed_files", tuple(self.managed_files))

    @classmethod
    def empty(cls, generated_on: date) -> WritingManifest:
        return cls(
            version=1,
            generated_at=generated_on.isoformat(),
            articles=MappingProxyType({}),
            managed_files=(),
        )


@dataclass(frozen=True, slots=True)
class CatalogResult:
    articles: tuple[WritingArticle, ...]
    issues: tuple[WritingIssue, ...]
