"""Deterministic private inspection and strict WeChat Reading plan I/O."""

from __future__ import annotations

from datetime import date
import hashlib
import hmac
from pathlib import Path
import re
from typing import Any
import unicodedata

import yaml

from shared.rendering import atomic_write_text
from writings.catalog import SLUG_PATTERN

from ..models import ExportInventory, WeReadImportError, private_import_path, WEREAD_NAMESPACE
from .markdown import parse_book_notes
from .models import BookNotes, WeReadArticlePlan, WeReadPlan
from .yaml_safety import BoundedSafeLoader


_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ARTICLE_FIELDS = {
    "source_ref", "detected_title", "detected_author", "include", "slug", "title", "published_at", "summary", "tags"
}
_REDACTION_KEY = b"weread-plan-v1"
_MAX_PLAN_BYTES = 8 * 1024 * 1024


class _SlugRegistry(set[str]):
    def __init__(self) -> None:
        super().__init__()
        self.next_suffix: dict[str, int] = {}


class _StrictLoader(BoundedSafeLoader):
    pass


class _PlanDumper(yaml.SafeDumper):
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


def _invalid(message: str) -> WeReadImportError:
    return WeReadImportError("invalid_plan", message)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not (cleaned := " ".join(value.split())):
        raise _invalid(f"{name} must be a non-empty plain-text string")
    if any(unicodedata.category(char).startswith("C") for char in cleaned):
        raise _invalid(f"{name} must be plain text")
    return cleaned


def _optional_date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise _invalid("published_at must be an ISO date or null")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise _invalid("published_at must be an ISO date or null") from error


def _article(value: Any) -> WeReadArticlePlan:
    if not isinstance(value, dict) or set(value) != _ARTICLE_FIELDS:
        raise _invalid("book records must contain exactly the supported fields")
    source_ref = _required_text(value["source_ref"], "source_ref")
    if not re.fullmatch(r"book:[0-9a-f]{16}", source_ref):
        raise _invalid("source_ref must be a redacted book digest")
    if type(value["include"]) is not bool:
        raise _invalid("include must be a YAML boolean")
    slug = _required_text(value["slug"], "slug")
    if not SLUG_PATTERN.fullmatch(slug):
        raise _invalid("slug must be lowercase ASCII kebab-case")
    detected_author = value["detected_author"]
    if detected_author is not None:
        detected_author = _required_text(detected_author, "detected_author")
    summary = value["summary"]
    if summary is not None:
        if not isinstance(summary, str) or "\n" in summary or "\r" in summary:
            raise _invalid("summary must be one-line plain text or null")
        summary = _required_text(summary, "summary")
        if "<" in summary or ">" in summary:
            raise _invalid("summary must be one-line plain text or null")
    tags = value["tags"]
    if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not SLUG_PATTERN.fullmatch(tag) for tag in tags):
        raise _invalid("tags must be a non-empty list of lowercase kebab-case strings")
    if len(tags) != len(set(tags)):
        raise _invalid("tags must be unique")
    return WeReadArticlePlan(
        source_ref=source_ref,
        detected_title=_required_text(value["detected_title"], "detected_title"),
        detected_author=detected_author,
        include=value["include"],
        slug=slug,
        title=_required_text(value["title"], "title"),
        published_at=_optional_date(value["published_at"]),
        summary=summary,
        tags=tuple(tags),
    )


def _validated_plan(value: Any) -> WeReadPlan:
    if not isinstance(value, dict) or set(value) != {"version", "source", "export_fingerprint", "books"}:
        raise _invalid("plan must contain exactly version, source, export_fingerprint, and books")
    if type(value["version"]) is not int or value["version"] != 1:
        raise _invalid("plan version is unsupported")
    if value["source"] != "wechat-reading-export":
        raise _invalid("plan source must be wechat-reading-export")
    fingerprint = value["export_fingerprint"]
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise _invalid("export_fingerprint must be a SHA-256 fingerprint")
    if not isinstance(value["books"], list):
        raise _invalid("books must be a list")
    books = tuple(_article(item) for item in value["books"])
    refs = [item.source_ref for item in books]
    slugs = [item.slug for item in books]
    if len(refs) != len(set(refs)) or len(slugs) != len(set(slugs)):
        raise _invalid("book source references and slugs must be unique")
    return WeReadPlan(1, "wechat-reading-export", fingerprint, books)


def _redacted_ref(identity: str) -> str:
    digest = hmac.new(_REDACTION_KEY, identity.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"book:{digest}"


def _slug_base(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def _suggest_slug(title: str, source_ref: str, used: set[str]) -> str:
    base = _slug_base(title) or f"book-{source_ref.split(':', 1)[1][:8]}"
    if base not in used:
        used.add(base)
        return base
    next_suffix = getattr(used, "next_suffix", None)
    if next_suffix is None:
        next_suffix = {}
    counter = next_suffix.get(base, 1)
    candidate = f"{base}-{counter}"
    while candidate in used:
        counter += 1
        candidate = f"{base}-{counter}"
    next_suffix[base] = counter + 1
    used.add(candidate)
    return candidate


def inspect_export(inventory: ExportInventory) -> WeReadPlan:
    """Inspect independent candidates; malformed candidates are intentionally omitted."""
    parsed: list[tuple[BookNotes, str]] = []
    for source_ref in inventory.markdown_paths:
        record = inventory.files[source_ref]
        try:
            book = parse_book_notes(record)
        except WeReadImportError:
            continue
        identity = f"book:{book.book_id}" if book.book_id else f"hash:{record.sha256}"
        parsed.append((book, identity))
    strong_counts: dict[str, int] = {}
    weak_counts: dict[str, int] = {}
    for _, identity in parsed:
        (strong_counts if identity.startswith("book:") else weak_counts)[identity] = (strong_counts if identity.startswith("book:") else weak_counts).get(identity, 0) + 1
    candidates = [
        (book, identity, _redacted_ref(identity))
        for book, identity in parsed
        if (strong_counts if identity.startswith("book:") else weak_counts)[identity] == 1
    ]
    used: set[str] = _SlugRegistry()
    books = tuple(
        WeReadArticlePlan(
            source_ref=redacted,
            detected_title=book.title,
            detected_author=book.author,
            include=False,
            slug=_suggest_slug(book.title, redacted, used),
            title=book.title,
            published_at=None,
            summary=None,
            tags=("reading",),
        )
        for book, _, redacted in candidates
    )
    return WeReadPlan(1, "wechat-reading-export", inventory.fingerprint, books)


def _plan_path(path: str | Path) -> Path:
    try:
        return private_import_path(path, WEREAD_NAMESPACE)
    except WeReadImportError as error:
        raise _invalid("plan must be below build/weread-import") from error


def load_plan(path: str | Path) -> WeReadPlan:
    target = _plan_path(path)
    try:
        raw = target.read_bytes()
        if len(raw) > _MAX_PLAN_BYTES:
            raise _invalid("plan exceeds the local safety limit")
        payload = raw.decode("utf-8", errors="strict")
        for token in yaml.scan(payload, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
                raise _invalid("plan aliases and tags are unsupported")
        return _validated_plan(yaml.load(payload, Loader=_StrictLoader))
    except WeReadImportError:
        raise
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        RecursionError,
        MemoryError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        raise _invalid("plan cannot be read") from error


def serialize_plan(plan: WeReadPlan) -> str:
    checked = _validated_plan(
        {
            "version": plan.version,
            "source": plan.source,
            "export_fingerprint": plan.export_fingerprint,
            "books": [
                {
                    "source_ref": book.source_ref,
                    "detected_title": book.detected_title,
                    "detected_author": book.detected_author,
                    "include": book.include,
                    "slug": book.slug,
                    "title": book.title,
                    "published_at": book.published_at,
                    "summary": book.summary,
                    "tags": list(book.tags),
                }
                for book in plan.books
            ],
        }
    )
    payload = {
        "version": checked.version,
        "source": checked.source,
        "export_fingerprint": checked.export_fingerprint,
        "books": [
            {
                "source_ref": book.source_ref,
                "detected_title": book.detected_title,
                "detected_author": book.detected_author,
                "include": book.include,
                "slug": book.slug,
                "title": book.title,
                "published_at": book.published_at,
                "summary": book.summary,
                "tags": list(book.tags),
            }
            for book in checked.books
        ],
    }
    return yaml.dump(payload, Dumper=_PlanDumper, allow_unicode=True, sort_keys=False, default_flow_style=False)


def write_plan(path: str | Path, plan: WeReadPlan) -> None:
    target = _plan_path(path)
    try:
        atomic_write_text(target, serialize_plan(plan))
    except WeReadImportError:
        raise
    except OSError as error:
        raise _invalid("unable to write private inspection plan") from error
