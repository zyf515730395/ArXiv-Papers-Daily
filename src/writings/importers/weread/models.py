"""Immutable private contracts for WeChat Reading inspection."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Literal, Mapping
import unicodedata


ONE_SENTENCE_MAX_CHARS = 200
SUMMARY_ITEM_MAX_CHARS = 500
_HTML = re.compile(
    r"<!--|<![^>]*>|<\?[^>]*\?>|</?[A-Za-z][^>]*>", re.IGNORECASE
)
_PRIVATE_PATH = re.compile(
    r"(?i)(?:\b[a-z]:[\\/]|\\\\[^\\/\r\n]+[\\/]|(?<![:/\w])/(?!/)[^\s<>]+)"
)
_SOURCE_FILENAME = re.compile(
    r"(?i)(?<![\w./\\@-])\.?[\w][\w .-]*?\.md(?![\w.-])"
)
_BOOK_ID = re.compile(
    r"(?i)\b(?:book(?:[_ -]?id)?\s*[:=]?\s*)?[0-9]{6,}\b"
)


def _summary_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("summary text violates length bounds")
    if value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError("summary text must be one trimmed line")
    if (
        _HTML.search(value)
        or _PRIVATE_PATH.search(value)
        or _SOURCE_FILENAME.search(value)
        or _BOOK_ID.search(value)
        or any(
            (category := unicodedata.category(character)).startswith("C")
            or category in {"Zl", "Zp"}
            for character in value
        )
    ):
        raise ValueError("summary text must be plain text")
    return value


def _summary_items(
    values: object, *, minimum: int, maximum: int
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not minimum <= len(values) <= maximum:
        raise ValueError("summary items violate count bounds")
    items = tuple(_summary_text(value, maximum=SUMMARY_ITEM_MAX_CHARS) for value in values)
    identities = {
        " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        for value in items
    }
    if len(identities) != len(items):
        raise ValueError("summary items must be unique")
    return items


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
class SummaryResult:
    """Validated structured synthesis safe for later private preview rendering."""

    one_sentence: str
    key_ideas: tuple[str, ...]
    reflections: tuple[str, ...]
    questions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "one_sentence",
            _summary_text(self.one_sentence, maximum=ONE_SENTENCE_MAX_CHARS),
        )
        object.__setattr__(
            self,
            "key_ideas",
            _summary_items(self.key_ideas, minimum=3, maximum=8),
        )
        object.__setattr__(
            self,
            "reflections",
            _summary_items(self.reflections, minimum=0, maximum=6),
        )
        object.__setattr__(
            self,
            "questions",
            _summary_items(self.questions, minimum=0, maximum=6),
        )


@dataclass(frozen=True, slots=True)
class SummaryConfig:
    """Per-preview local model settings; never serialized into public output."""

    model: str
    base_url: str = "http://127.0.0.1:11434/v1"
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, str)
            or not self.model
            or self.model != self.model.strip()
            or len(self.model) > 200
            or any(
                unicodedata.category(character).startswith("C")
                for character in self.model
            )
        ):
            raise ValueError("model must be trimmed plain text of at most 200 characters")
        if not isinstance(self.base_url, str) or not self.base_url:
            raise ValueError("base URL must be non-empty")
        if not isinstance(self.timeout, (int, float)) or not 0 < self.timeout <= 300:
            raise ValueError("timeout must be between zero and 300 seconds")


@dataclass(frozen=True, slots=True)
class SummaryCacheKey:
    """Content-addressed identity for one selected book/model contract."""

    source_fingerprint: str
    content_fingerprint: str
    prompt_version: str
    model: str
    transport_version: str
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        hexadecimal = set("0123456789abcdef")
        source_digest = self.source_fingerprint.removeprefix("sha256:")
        if (
            not self.source_fingerprint.startswith("sha256:")
            or len(source_digest) != 64
            or any(character not in hexadecimal for character in source_digest)
            or len(self.content_fingerprint) != 64
            or any(
                character not in hexadecimal for character in self.content_fingerprint
            )
        ):
            raise ValueError("summary fingerprints must be lowercase SHA-256 values")
        for value in (self.prompt_version, self.model, self.transport_version):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 200
                or value != value.strip()
                or any(unicodedata.category(char).startswith("C") for char in value)
            ):
                raise ValueError("summary cache identity text is invalid")
        inputs = {
            "source_fingerprint": self.source_fingerprint,
            "content_fingerprint": self.content_fingerprint,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "transport_version": self.transport_version,
        }
        encoded = json.dumps(
            inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        object.__setattr__(self, "digest", hashlib.sha256(encoded).hexdigest())


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
