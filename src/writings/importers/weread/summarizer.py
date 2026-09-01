"""Structured map-reduce orchestration for normalized WeRead book notes."""

from __future__ import annotations

import json
import unicodedata
from typing import Iterable

from writings.importers.models import WeReadImportError

from .cache import SummaryCache
from .client import LoopbackChatClient
from .models import BookNotes, SummaryConfig, SummaryResult
from .privacy import guard_summary
from .prompts import (
    build_map_chunks,
    build_reduce_batches,
    map_messages,
    reduce_messages,
)


_SUMMARY_FIELDS = {"one_sentence", "key_ideas", "reflections", "questions"}
_COPY_GUARD_CHARS = 120


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _normalized_copy_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def _segments(result: SummaryResult) -> Iterable[str]:
    yield result.one_sentence
    yield from result.key_ideas
    yield from result.reflections
    yield from result.questions


def _guard_highlight_copy(
    result: SummaryResult, highlights: tuple[str, ...]
) -> None:
    normalized_highlights = tuple(_normalized_copy_text(value) for value in highlights)
    for segment in _segments(result):
        normalized_segment = _normalized_copy_text(segment)
        if len(normalized_segment) < _COPY_GUARD_CHARS:
            continue
        for start in range(len(normalized_segment) - _COPY_GUARD_CHARS + 1):
            run = normalized_segment[start : start + _COPY_GUARD_CHARS]
            if any(run in highlight for highlight in normalized_highlights):
                raise WeReadImportError(
                    "copyright_guard",
                    "generated summary copies too much source highlight text",
                )


def parse_summary(
    raw: str,
    highlights: tuple[str, ...],
    *,
    thoughts: tuple[str, ...] = (),
) -> SummaryResult:
    """Parse the exact JSON contract and guard only source-authored highlights."""
    del thoughts
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise WeReadImportError(
            "invalid_summary", "model summary is not the required JSON object"
        ) from None
    if not isinstance(value, dict) or set(value) != _SUMMARY_FIELDS:
        raise WeReadImportError(
            "invalid_summary", "model summary has unexpected fields"
        )
    try:
        result = SummaryResult(
            one_sentence=value["one_sentence"],
            key_ideas=value["key_ideas"],
            reflections=value["reflections"],
            questions=value["questions"],
        )
    except (TypeError, ValueError):
        raise WeReadImportError(
            "invalid_summary", "model summary violates the structured contract"
        ) from None
    _guard_highlight_copy(result, highlights)
    return result


def _section_items(book: BookNotes, name: str) -> tuple[str, ...]:
    return tuple(
        text
        for section in book.sections
        if section.name == name
        for _chapter, text in section.items
    )


def summarize_book(
    book: BookNotes,
    config: SummaryConfig,
    cache: SummaryCache,
    refresh: bool = False,
) -> SummaryResult:
    """Return one validated cached or freshly generated structured summary."""
    key = cache.key_for(book, config.model)
    highlights = _section_items(book, "highlights")
    thoughts = _section_items(book, "thoughts")
    if not refresh:
        cached = cache.load(key)
        if cached is not None:
            _guard_highlight_copy(cached, highlights)
            guard_summary(book, cached)
            return cached

    client = LoopbackChatClient(config.base_url)
    mapped: list[SummaryResult] = []
    for chunk in build_map_chunks(book):
        raw = client.complete(
            map_messages(book, chunk), model=config.model, timeout=config.timeout
        )
        mapped_result = parse_summary(raw, highlights, thoughts=thoughts)
        guard_summary(book, mapped_result)
        mapped.append(mapped_result)
    level = tuple(mapped)
    while len(level) > 1:
        reduced: list[SummaryResult] = []
        reduced_count = 0
        for batch in build_reduce_batches(level):
            if len(batch) == 1:
                reduced.append(batch[0])
                continue
            raw = client.complete(
                reduce_messages(batch), model=config.model, timeout=config.timeout
            )
            reduced_result = parse_summary(raw, highlights, thoughts=thoughts)
            guard_summary(book, reduced_result)
            reduced.append(reduced_result)
            reduced_count += len(batch) - 1
        if reduced_count == 0:
            raise WeReadImportError(
                "summary_payload_too_large",
                "structured summaries cannot fit a bounded reduction request",
            )
        level = tuple(reduced)
    result = level[0]
    cache.store(key, result, book)
    return result
