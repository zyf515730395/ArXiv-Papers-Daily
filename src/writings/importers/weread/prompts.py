"""Fixed prompts and deterministic item-boundary chunking for WeRead notes."""

from __future__ import annotations

import json
from typing import Mapping, Sequence

from writings.importers.models import WeReadImportError

from .models import BookNotes, SummaryResult


PROMPT_VERSION = "weread-summary-v2"
TRANSPORT_VERSION = "openai-chat-v2"
# Canonical JSON character count of the complete messages array, including roles,
# system text, book metadata, instructions, and source or reduction content.
MAX_MESSAGE_CHARS = 24_000
MAX_CHUNK_CHARS = MAX_MESSAGE_CHARS

_SYSTEM_PROMPT = """你是读书笔记整理助手。只根据提供的笔记进行综合，不得编造笔记中没有的事实。\n请使用中文，避免长段引用原文，并且只返回符合指定结构的 JSON 对象。"""
_MAP_INSTRUCTION = """综合下面这一批完整笔记条目。返回且只返回 JSON：\n{"one_sentence":"一句话","key_ideas":["3到8项"],"reflections":["0到6项"],"questions":["0到6项"]}\n笔记：\n"""
_REDUCE_INSTRUCTION = """按给定顺序综合下面各批摘要，不得遗漏、调换或添加来源中不存在的事实。
避免长段引用。返回且只返回相同结构的 JSON。
批次摘要：
"""


def _format_item(section: str, chapter: str | None, text: str) -> str:
    chapter_name = chapter or "未分章"
    return f"[{section}][{chapter_name}]\n{text}"


def message_character_count(messages: Sequence[Mapping[str, str]]) -> int:
    """Count characters in the exact canonical JSON messages payload."""
    return len(
        json.dumps(list(messages), ensure_ascii=False, separators=(",", ":"))
    )


def _encoded_text_chars(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) - 2


def build_map_chunks(book: BookNotes) -> tuple[str, ...]:
    """Pack complete items while bounding each full map messages payload."""
    fixed_length = message_character_count(map_messages(book, ""))
    if fixed_length >= MAX_MESSAGE_CHARS:
        raise WeReadImportError(
            "summary_context_too_large",
            "book metadata exceeds the summary request boundary",
        )
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    separator_length = _encoded_text_chars("\n\n")
    for section in book.sections:
        for chapter, text in section.items:
            item = _format_item(section.name, chapter, text)
            item_length = _encoded_text_chars(item)
            if fixed_length + item_length > MAX_MESSAGE_CHARS:
                raise WeReadImportError(
                    "source_item_too_large",
                    "one normalized note item exceeds the summary boundary",
                )
            separator = separator_length if current else 0
            if (
                current
                and fixed_length + current_length + separator + item_length
                > MAX_MESSAGE_CHARS
            ):
                chunks.append("\n\n".join(current))
                current = []
                current_length = 0
                separator = 0
            current.append(item)
            current_length += separator + item_length
    if current:
        chunks.append("\n\n".join(current))
    if not chunks:
        raise WeReadImportError(
            "summary_source_empty", "selected book has no recognized note items"
        )
    return tuple(chunks)


def map_messages(book: BookNotes, chunk: str) -> tuple[dict[str, str], ...]:
    context = f"书名：{book.title}\n作者：{book.author or '未知'}\n"
    return (
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context + _MAP_INSTRUCTION + chunk},
    )


def reduce_messages(results: tuple[SummaryResult, ...]) -> tuple[dict[str, str], ...]:
    ordered = [
        {
            "one_sentence": result.one_sentence,
            "key_ideas": list(result.key_ideas),
            "reflections": list(result.reflections),
            "questions": list(result.questions),
        }
        for result in results
    ]
    content = _REDUCE_INSTRUCTION + json.dumps(
        ordered, ensure_ascii=False, separators=(",", ":")
    )
    return (
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": content},
    )


def build_reduce_batches(
    results: tuple[SummaryResult, ...],
) -> tuple[tuple[SummaryResult, ...], ...]:
    """Greedily pack consecutive summaries under the full messages cap."""
    batches: list[tuple[SummaryResult, ...]] = []
    current: list[SummaryResult] = []
    for result in results:
        if message_character_count(reduce_messages((result,))) > MAX_MESSAGE_CHARS:
            raise WeReadImportError(
                "summary_payload_too_large",
                "one structured summary exceeds the reduction boundary",
            )
        candidate = (*current, result)
        if (
            current
            and message_character_count(reduce_messages(candidate))
            > MAX_MESSAGE_CHARS
        ):
            batches.append(tuple(current))
            current = [result]
        else:
            current.append(result)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


__all__ = [
    "MAX_CHUNK_CHARS",
    "MAX_MESSAGE_CHARS",
    "PROMPT_VERSION",
    "TRANSPORT_VERSION",
    "build_map_chunks",
    "build_reduce_batches",
    "map_messages",
    "message_character_count",
    "reduce_messages",
]
