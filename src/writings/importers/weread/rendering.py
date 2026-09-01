"""Deterministic public bundle rendering for reviewed WeRead summaries."""

from __future__ import annotations

import html
import json
from pathlib import Path

from shared.rendering import atomic_write_text
from writings import WritingArticle, validate_writing_bundle

from .models import BookNotes, SummaryResult, WeReadArticlePlan


_MARKDOWN_PLAIN = frozenset("\\`*_{}[]()#+-.!|>")


def _yaml_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _plain_markdown(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return "".join(
        "\\" + character if character in _MARKDOWN_PLAIN else character
        for character in escaped
    )


def _public_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    ordered = ("reading", *tags)
    return tuple(dict.fromkeys(ordered))


def _body(
    author: str | None,
    summary: SummaryResult,
) -> str:
    blocks: list[str] = []
    if author:
        blocks.append(f"作者：{_plain_markdown(author)}")
    sections = (
        ("一句话", (summary.one_sentence,), False),
        ("核心观点", summary.key_ideas, True),
        ("我的思考", summary.reflections, True),
        ("留给自己的问题", summary.questions, True),
    )
    for heading, items, listed in sections:
        if not items:
            continue
        content = "\n".join(
            f"- {_plain_markdown(item)}" if listed else _plain_markdown(item)
            for item in items
        )
        blocks.append(f"## {heading}\n\n{content}")
    return "\n\n".join(blocks) + "\n"


def render_public_bundle(
    plan: WeReadArticlePlan,
    book: BookNotes,
    summary: SummaryResult,
    bundle_root: str | Path,
) -> WritingArticle:
    """Write and strictly validate one synthesis-only public writing bundle."""
    del book  # Raw normalized notes are deliberately outside the renderer boundary.
    if plan.published_at is None:
        raise ValueError("published_at is required for an included book")
    tags = _public_tags(plan.tags)
    front_matter = "\n".join(
        (
            "---",
            f"title: {_yaml_text(plan.title)}",
            f"slug: {plan.slug}",
            f"published_at: {plan.published_at}",
            "kind: book-note",
            "public: true",
            f"summary: {_yaml_text(plan.summary or summary.one_sentence)}",
            f"tags: [{', '.join(tags)}]",
            "source: wechat-reading",
            "---",
            "",
        )
    )
    target = Path(bundle_root)
    atomic_write_text(
        target / "index.md",
        front_matter + "\n" + _body(plan.detected_author, summary),
    )
    return validate_writing_bundle(target)


__all__ = ["render_public_bundle"]
