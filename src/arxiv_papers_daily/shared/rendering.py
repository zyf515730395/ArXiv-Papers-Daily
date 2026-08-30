"""Safe Markdown rendering and atomic text output helpers."""

from __future__ import annotations

import os
from pathlib import Path
import re

import bleach
import markdown


ALLOWED_HTML_TAGS = {
    "a",
    "article",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "ul",
}
ALLOWED_HTML_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "article": ["id", "class"],
}


def atomic_write_text(path: str | Path, content: str) -> None:
    """Replace a UTF-8 text file without exposing a partial write."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)


def render_note_content(markdown_content: str) -> str:
    """Render Markdown to a sanitized HTML fragment."""
    display_content = re.sub(
        r"\A---\s*\n.*?\n---\s*\n",
        "",
        markdown_content,
        count=1,
        flags=re.DOTALL,
    )
    rendered = markdown.markdown(display_content, extensions=["extra", "sane_lists"])
    cleaned = bleach.clean(
        rendered,
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRIBUTES,
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    return bleach.linkify(cleaned)
