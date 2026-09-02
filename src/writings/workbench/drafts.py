"""Private original-draft creation and lifecycle operations."""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path

from shared.rendering import atomic_write_text
from writings.catalog import SLUG_PATTERN, SUPPORTED_KINDS
from writings.importers.models import PROJECT_ROOT

from .models import WorkbenchError
from .paths import draft_root


def _portable_collision(parent: Path, name: str) -> bool:
    try:
        return any(entry.name.casefold() == name.casefold() for entry in parent.iterdir())
    except FileNotFoundError:
        return False
    except OSError as error:
        raise WorkbenchError("private_io_failed", "unable to inspect private drafts") from error


def _template(slug: str, title: str, kind: str, published_at: date) -> str:
    title_scalar = json.dumps(title, ensure_ascii=False)
    return (
        "---\n"
        f"title: {title_scalar}\n"
        f"slug: {slug}\n"
        f"published_at: {published_at.isoformat()}\n"
        f"kind: {kind}\n"
        "public: true\n"
        'summary: ""\n'
        "tags: []\n"
        "source: original\n"
        "---\n\n"
        f"# {title}\n\n"
        "在这里开始整理正文。\n"
    )


def create_draft(slug: str, title: str, kind: str, published_at: date) -> Path:
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise WorkbenchError("invalid_slug", "slug must be lowercase ASCII kebab-case")
    if kind not in SUPPORTED_KINDS:
        raise WorkbenchError("invalid_kind", "kind must be learning-note or book-note")
    if not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title:
        raise WorkbenchError("invalid_title", "title must be non-empty single-line text")
    if not isinstance(published_at, date):
        raise WorkbenchError("invalid_date", "date must be a valid ISO date")

    root = draft_root()
    public_root = Path(PROJECT_ROOT) / "content" / "writings"
    if _portable_collision(public_root, slug):
        raise WorkbenchError("occupied_slug", "a public article already uses this slug")
    if _portable_collision(root, slug):
        raise WorkbenchError("draft_exists", "draft already exists; choose another slug")

    bundle = root / slug
    try:
        root.mkdir(parents=True, exist_ok=True)
        bundle.mkdir()
        atomic_write_text(bundle / "index.md", _template(slug, title.strip(), kind, published_at))
    except FileExistsError as error:
        raise WorkbenchError("draft_exists", "draft already exists; choose another slug") from error
    except OSError as error:
        try:
            if bundle.is_dir() and not any(bundle.iterdir()):
                bundle.rmdir()
        except OSError:
            pass
        raise WorkbenchError("private_io_failed", "unable to create private draft") from error
    return bundle
