"""Bounded local normalization for common WeChat Reading Markdown exports."""

from __future__ import annotations

import re
from typing import Any
import unicodedata

import yaml

from ..models import ExportFile, WeReadImportError
from .models import BookNotes, NoteSection


_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_FRONT_MATTER_BYTES = 256 * 1024
_TITLE_KEYS = ("title", "book", "bookName")
_AUTHOR_KEYS = ("author", "authors", "writer")
_BOOK_ID_KEYS = ("bookId", "book_id")
_CATEGORY_KEYS = ("category",)
_UPDATED_KEYS = ("lastNoteUpdate", "updated", "updated_at")
_METADATA_KEYS = _TITLE_KEYS + _AUTHOR_KEYS + _BOOK_ID_KEYS + _CATEGORY_KEYS + _UPDATED_KEYS
_SECTION_ALIASES = {
    "划线": "highlights",
    "划线笔记": "highlights",
    "高亮": "highlights",
    "高亮划线": "highlights",
    "想法": "thoughts",
    "我的想法": "thoughts",
    "读书笔记": "thoughts",
    "书评": "reviews",
    "本书评论": "reviews",
    "点评": "reviews",
}
_WEREAD_URL = re.compile(r"https?://(?:[\w.-]*\.)?(?:weread|wereadapp)\.qq\.com/[^\s)>]*", re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_ITEM = re.compile(r"^[ \t]*(?:[-*+])[ \t]+(.+?)\s*$")
_CALLOUT = re.compile(r"^[ \t]*>[ \t]*([A-Za-z_][A-Za-z0-9_]*|书名|作者)[：:]\s*(.+?)\s*$")


class _StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[Any, Any]:
    value: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in value:
            raise yaml.YAMLError("duplicate mapping key")
        value[key] = loader.construct_object(value_node, deep=deep)
    return value


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _error(code: str, message: str) -> WeReadImportError:
    return WeReadImportError(code, message)


def _clean(value: object) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = _WEREAD_URL.sub("", str(value))
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    size = len(lines[0].encode("utf-8"))
    closing = None
    for index, line in enumerate(lines[1:], start=1):
        size += len(line.encode("utf-8"))
        if size > _MAX_FRONT_MATTER_BYTES:
            raise _error("invalid_metadata", "front matter exceeds the local safety limit")
        if line.strip() in {"---", "..."}:
            closing = index
            break
    if closing is None:
        raise _error("invalid_metadata", "front matter is not terminated")
    payload = "".join(lines[1:closing])
    try:
        for token in yaml.scan(payload, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
                raise _error("invalid_metadata", "front matter aliases and tags are unsupported")
        decoded = yaml.load(payload, Loader=_StrictLoader)
    except WeReadImportError:
        raise
    except (yaml.YAMLError, TypeError, ValueError) as error:
        raise _error("invalid_metadata", "front matter is invalid") from error
    if decoded is None:
        decoded = {}
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise _error("invalid_metadata", "front matter must be a mapping")
    return decoded, "".join(lines[closing + 1 :])


def _strip_html_comments(text: str) -> str:
    """Remove complete or dangling comments before Markdown is split into lines."""
    parts: list[str] = []
    position = 0
    while (start := text.find("<!--", position)) >= 0:
        parts.append(text[position:start])
        end = text.find("-->", start + 4)
        comment_end = len(text) if end < 0 else end + 3
        parts.append(
            "".join(
                char if char in "\r\n" else " "
                for char in text[start:comment_end]
            )
        )
        position = comment_end
    parts.append(text[position:])
    return "".join(parts)


def _first(metadata: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _clean(metadata.get(key))
        if value:
            return value
    return None


def _callouts(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        match = _CALLOUT.match(line)
        if not match:
            continue
        raw_key = {"书名": "title", "作者": "author"}.get(match.group(1), match.group(1))
        if raw_key in _METADATA_KEYS and raw_key not in values:
            value = _clean(match.group(2))
            if value:
                values[raw_key] = value
    return values


def _section_name(value: str) -> str | None:
    return _SECTION_ALIASES.get(_clean(value))


def parse_book_notes(record: ExportFile) -> BookNotes:
    """Normalize one inventoried Markdown record without exposing source text."""
    try:
        raw = record.source_path.read_bytes()
    except OSError as error:
        raise _error("unreadable_source", "unable to read a local Markdown candidate") from error
    if len(raw) > _MAX_BODY_BYTES:
        raise _error("invalid_markdown", "Markdown candidate exceeds the local safety limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _error("invalid_utf8", "Markdown candidate is not valid UTF-8") from error
    metadata, body = _front_matter(_strip_html_comments(text))
    lines = body.splitlines()
    callouts = _callouts(lines)
    front_title = _first(metadata, _TITLE_KEYS)
    callout_title = _first(callouts, _TITLE_KEYS)
    h1_title: str | None = None
    for line in lines:
        heading = _HEADING.match(line)
        if heading and len(heading.group(1)) == 1 and not _section_name(heading.group(2)):
            h1_title = _clean(heading.group(2)) or None
            if h1_title:
                break
    title = front_title or callout_title or h1_title
    if not title:
        raise _error("missing_metadata", "book title is unavailable")
    warnings: list[str] = []
    other_titles = [item for item in (callout_title, h1_title) if item and item != title]
    if front_title and other_titles:
        warnings.append("conflicting_title")
    merged = {**callouts, **{key: _clean(value) for key, value in metadata.items() if key in _METADATA_KEYS}}
    author = _first(merged, _AUTHOR_KEYS)
    book_id = _first(merged, _BOOK_ID_KEYS)
    category = _first(merged, _CATEGORY_KEYS)
    updated_at = _first(merged, _UPDATED_KEYS)
    sections: dict[str, list[tuple[str | None, str]]] = {"highlights": [], "thoughts": [], "reviews": []}
    active: str | None = None
    active_level = 0
    chapter: str | None = None
    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            label = _clean(heading.group(2))
            known = _section_name(label)
            if known:
                active = known
                active_level = level
                chapter = None
            elif active is not None and level > active_level:
                chapter = label or None
            elif active is not None and level <= active_level:
                active = None
                chapter = None
            continue
        if active is None:
            continue
        item = _ITEM.match(line)
        if item:
            value = _clean(item.group(1))
            if value:
                sections[active].append((chapter, value))
    return BookNotes(
        source_ref=record.relative_path.as_posix(),
        source_fingerprint="sha256:" + record.sha256,
        title=title,
        author=author,
        book_id=book_id,
        category=category,
        updated_at=updated_at,
        sections=tuple(NoteSection(name, tuple(items)) for name, items in sections.items() if items),
        metadata={key: value for key, value in merged.items() if value},
        warnings=tuple(warnings),
    )
