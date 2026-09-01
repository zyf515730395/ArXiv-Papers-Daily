"""Contextual private-identifier guards for generated and public WeRead text."""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Iterable
import unicodedata

from writings.importers.models import WeReadImportError

from .models import BookNotes, SummaryResult


_MIN_WEAK_IDENTIFIER_CHARS = 6


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("\\", "/")


def _identifier_variants(book: BookNotes) -> tuple[tuple[str, ...], tuple[str, ...]]:
    path = PurePosixPath(book.source_ref)
    strong = {_normalized(book.source_ref), _normalized(path.name)}
    if book.book_id:
        strong.add(_normalized(book.book_id))

    weak: set[str] = set()
    for part in path.parts:
        normalized_part = _normalized(part)
        stem = _normalized(PurePosixPath(part).stem)
        for value in (normalized_part, stem):
            if value:
                weak.add(value)
    weak.difference_update(strong)
    return (
        tuple(sorted((value for value in strong if value), key=len, reverse=True)),
        tuple(sorted((value for value in weak if value), key=len, reverse=True)),
    )


def _contains(text: str, identifier: str) -> bool:
    if any(not character.isalnum() for character in identifier):
        return identifier in text
    return re.search(
        rf"(?<![\w]){re.escape(identifier)}(?![\w])", text, re.UNICODE
    ) is not None


def _contains_weak(text: str, identifier: str) -> bool:
    distinctive = (
        len(identifier) >= _MIN_WEAK_IDENTIFIER_CHARS
        or any(character.isdigit() for character in identifier)
        or any(not character.isalnum() for character in identifier)
    )
    if distinctive:
        return _contains(text, identifier)
    start = 0
    end = len(text)
    while start < end and not text[start].isalnum():
        start += 1
    while end > start and not text[end - 1].isalnum():
        end -= 1
    return text[start:end] == identifier


def guard_private_text(book: BookNotes, values: Iterable[str | None]) -> None:
    """Reject only identifiers belonging to the current private source."""
    strong, weak = _identifier_variants(book)
    for value in values:
        if not value:
            continue
        normalized = _normalized(value)
        if any(_contains(normalized, identifier) for identifier in strong) or any(
            _contains_weak(normalized, identifier) for identifier in weak
        ):
            raise WeReadImportError(
                "private_identifier",
                "generated public text contains a private source identifier",
            )


def guard_summary(book: BookNotes, result: SummaryResult) -> None:
    guard_private_text(
        book,
        (
            result.one_sentence,
            *result.key_ideas,
            *result.reflections,
            *result.questions,
        ),
    )


__all__ = ["guard_private_text", "guard_summary"]
