"""Strict private review evidence for exact original-draft publication."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from shared.rendering import atomic_write_text
from writings.catalog import SLUG_PATTERN

from .models import WorkbenchError
from .paths import review_path


_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


def _strict_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkbenchError("invalid_state", "review state contains duplicate fields")
        result[key] = value
    return result


def load_reviews() -> dict[str, dict[str, str]]:
    path = review_path()
    if not os.path.lexists(path):
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_mapping)
    except WorkbenchError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkbenchError("invalid_state", "unable to load private review state") from error
    if not isinstance(payload, dict) or set(payload) != {"version", "articles"} or payload["version"] != 1:
        raise WorkbenchError("invalid_state", "private review state has an unsupported schema")
    articles = payload["articles"]
    if not isinstance(articles, dict):
        raise WorkbenchError("invalid_state", "private review articles must be a mapping")
    checked: dict[str, dict[str, str]] = {}
    for slug, record in articles.items():
        expected_page = f"previews/original/{slug}/index.html"
        if (
            not isinstance(slug, str)
            or not SLUG_PATTERN.fullmatch(slug)
            or not isinstance(record, dict)
            or set(record) != {"preview_fingerprint", "preview_page"}
            or not isinstance(record["preview_fingerprint"], str)
            or not _FINGERPRINT.fullmatch(record["preview_fingerprint"])
            or record["preview_page"] != expected_page
        ):
            raise WorkbenchError("invalid_state", "private review state contains an invalid article")
        checked[slug] = dict(record)
    return checked


def write_reviews(articles: dict[str, dict[str, str]]) -> None:
    checked = {slug: articles[slug] for slug in sorted(articles)}
    payload = json.dumps(
        {"version": 1, "articles": checked},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"
    try:
        atomic_write_text(review_path(), payload)
        load_reviews()
    except WorkbenchError:
        raise
    except OSError as error:
        raise WorkbenchError("private_io_failed", "unable to persist private review state") from error
