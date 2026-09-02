"""Deterministic index over existing private source previews."""

from __future__ import annotations

from html import escape
import os
from pathlib import Path
import posixpath

from shared.rendering import atomic_write_text
from writings.importers.models import PROJECT_ROOT

from .models import WorkbenchError
from .paths import preview_root
from .state import load_reviews


def _relative_link(index: Path, target: Path) -> str:
    project = Path(PROJECT_ROOT).resolve()
    resolved = target.resolve()
    if not resolved.is_relative_to(project / "build") or not resolved.is_file():
        raise WorkbenchError("unsafe_preview", "private preview page is unavailable")
    return posixpath.relpath(resolved.as_posix(), index.parent.resolve().as_posix())


def rebuild_preview_index() -> Path:
    index = preview_root() / "index.html"
    rows: list[str] = []
    try:
        reviews = load_reviews()
        for slug, record in sorted(reviews.items()):
            target = Path(PROJECT_ROOT) / "build" / "writings-workbench" / Path(
                *Path(record["preview_page"]).parts
            )
            try:
                href = _relative_link(index, target)
            except WorkbenchError:
                rows.append(
                    f'<li><strong>Original</strong> · {escape(slug)} · attention · '
                    "rerun preview</li>"
                )
            else:
                rows.append(
                    f'<li><a href="{escape(href, quote=True)}">Original · '
                    f"{escape(slug)}</a> · ready</li>"
                )

        for label, namespace in (("Notion", "notion-import"), ("WeChat Reading", "weread-import")):
            target = Path(PROJECT_ROOT) / "build" / namespace / "preview" / "index.html"
            if os.path.lexists(target):
                try:
                    href = _relative_link(index, target)
                except WorkbenchError:
                    rows.append(
                        f"<li><strong>{escape(label)}</strong> · attention · rerun preview</li>"
                    )
                else:
                    rows.append(
                        f'<li><a href="{escape(href, quote=True)}">{escape(label)}</a> · ready</li>'
                    )

        body = "\n".join(rows) if rows else "<li>No private previews yet.</li>"
        document = (
            "<!doctype html>\n"
            '<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Local writing previews</title>"
            "<style>body{font:16px/1.6 system-ui;max-width:52rem;margin:4rem auto;padding:0 1.5rem}"
            "li{margin:.6rem 0}a{color:#1857a4}</style></head><body>"
            "<h1>Local writing previews</h1><p>Private review index. Do not publish this directory.</p>"
            f"<ul>{body}</ul></body></html>\n"
        )
        atomic_write_text(index, document)
        return index
    except WorkbenchError:
        raise
    except OSError as error:
        raise WorkbenchError("preview_failed", "unable to rebuild private preview index") from error
