"""Deterministic private plans for offline Notion export inspection."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any
import unicodedata

import yaml

from shared.rendering import atomic_write_text
from writings.catalog import SLUG_PATTERN, SUPPORTED_KINDS

from .models import ExportInventory, ImportArticlePlan, ImportPlan, NotionImportError


_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_NOTION_ID = re.compile(r"(?i)\b[0-9a-f]{32}\b")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_H1 = re.compile(r"\A[ \t]*#[ \t]+(.+?)[ \t]*(?:#+[ \t]*)?(?:\r?\n|\Z)")
_ARTICLE_FIELDS = {
    "source_ref",
    "detected_title",
    "include",
    "slug",
    "title",
    "published_at",
    "kind",
    "summary",
    "tags",
}


def _invalid(message: str) -> NotionImportError:
    return NotionImportError("invalid_plan", message)


def _source_ref(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _invalid("source reference must be a non-empty relative POSIX path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or posix == PurePosixPath(".")
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.as_posix() != value
    ):
        raise _invalid("source reference must be a normalized relative POSIX path")
    return posix.as_posix()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a non-empty string")
    return value.strip()


def _optional_date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise _invalid("published_at must be an ISO date or null")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise _invalid("published_at must be an ISO date or null") from error


def _article(value: Any) -> ImportArticlePlan:
    if not isinstance(value, dict) or set(value) != _ARTICLE_FIELDS:
        raise _invalid("article records must contain exactly the supported fields")
    source_ref = _source_ref(value["source_ref"])
    detected_title = _required_text(value["detected_title"], "detected_title")
    if type(value["include"]) is not bool:
        raise _invalid("include must be a YAML boolean")
    slug = _required_text(value["slug"], "slug")
    if not SLUG_PATTERN.fullmatch(slug):
        raise _invalid("slug must be lowercase ASCII kebab-case")
    title = _required_text(value["title"], "title")
    published_at = _optional_date(value["published_at"])
    kind = value["kind"]
    if kind is not None and (not isinstance(kind, str) or kind not in SUPPORTED_KINDS):
        raise _invalid("kind must be a supported value or null")
    summary = value["summary"]
    if summary is not None:
        summary = _required_text(summary, "summary")
        if "\n" in summary or "\r" in summary or "<" in summary or ">" in summary:
            raise _invalid("summary must be one-line plain text or null")
    tags_value = value["tags"]
    if not isinstance(tags_value, list) or any(
        not isinstance(tag, str) or not SLUG_PATTERN.fullmatch(tag) for tag in tags_value
    ):
        raise _invalid("tags must be a list of lowercase kebab-case strings")
    if len(tags_value) != len(set(tags_value)):
        raise _invalid("tags must be unique")
    return ImportArticlePlan(
        source_ref=source_ref,
        detected_title=detected_title,
        include=value["include"],
        slug=slug,
        title=title,
        published_at=published_at,
        kind=kind,
        summary=summary,
        tags=tuple(tags_value),
    )


def _validated_plan(value: Any) -> ImportPlan:
    if not isinstance(value, dict) or set(value) != {"version", "source", "export_fingerprint", "articles"}:
        raise _invalid("plan must contain exactly version, source, export_fingerprint, and articles")
    if type(value["version"]) is not int or value["version"] != 1:
        raise _invalid("plan version is unsupported")
    if value["source"] != "notion-export":
        raise _invalid("plan source must be notion-export")
    fingerprint = value["export_fingerprint"]
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise _invalid("export_fingerprint must be a SHA-256 fingerprint")
    if not isinstance(value["articles"], list):
        raise _invalid("articles must be a list")
    articles = tuple(_article(item) for item in value["articles"])
    refs = [item.source_ref for item in articles]
    slugs = [item.slug for item in articles]
    if len(refs) != len(set(refs)) or len(slugs) != len(set(slugs)):
        raise _invalid("article source references and slugs must be unique")
    if refs != sorted(refs):
        raise _invalid("articles must be ordered by source reference")
    return ImportPlan(1, "notion-export", fingerprint, articles)


class _PlanDumper(yaml.SafeDumper):
    pass


class _StrictPlanLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _invalid("plan contains duplicate YAML fields")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictPlanLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _represent_string(dumper: yaml.Dumper, value: str) -> yaml.nodes.ScalarNode:
    style = "'" if _ISO_DATE.fullmatch(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_PlanDumper.add_representer(str, _represent_string)


def load_import_plan(path: str | Path) -> ImportPlan:
    """Load one strict, private import plan without accepting implicit YAML types."""
    try:
        payload = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_StrictPlanLoader)
    except (OSError, UnicodeError, yaml.YAMLError, NotionImportError) as error:
        raise _invalid("unable to load import plan") from error
    return _validated_plan(payload)


def serialize_import_plan(plan: ImportPlan) -> str:
    """Serialize plan fields and articles in a stable, review-friendly YAML order."""
    checked = _validated_plan(
        {
            "version": plan.version,
            "source": plan.source,
            "export_fingerprint": plan.export_fingerprint,
            "articles": [asdict(article) | {"tags": list(article.tags)} for article in plan.articles],
        }
    )
    payload = {
        "version": checked.version,
        "source": checked.source,
        "export_fingerprint": checked.export_fingerprint,
        "articles": [
            {
                "source_ref": article.source_ref,
                "detected_title": article.detected_title,
                "include": article.include,
                "slug": article.slug,
                "title": article.title,
                "published_at": article.published_at,
                "kind": article.kind,
                "summary": article.summary,
                "tags": list(article.tags),
            }
            for article in checked.articles
        ],
    }
    return yaml.dump(payload, Dumper=_PlanDumper, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _detected_title(source_path: Path, source_ref: str) -> str:
    try:
        text = source_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise NotionImportError("unsafe_archive", "unable to read Markdown candidate") from error
    match = _H1.match(text)
    if match:
        value = unicodedata.normalize("NFKC", match.group(1)).strip().rstrip("#").strip()
        if value:
            return value
    filename = unicodedata.normalize("NFKC", PurePosixPath(source_ref).stem)
    filename = _NOTION_ID.sub("", filename).strip(" -_\t")
    return " ".join(filename.split()) or "Untitled"


def _slug_base(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower()
    value = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return value


def _suggest_slug(title: str, source_ref: str, used: set[str]) -> str:
    base = _slug_base(title)
    suffix = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:8]
    candidate = base or f"notion-{suffix}"
    if candidate in used:
        candidate = f"{base or 'notion'}-{suffix}"
    while candidate in used:
        suffix = hashlib.sha256((source_ref + candidate).encode("utf-8")).hexdigest()[:8]
        candidate = f"{base or 'notion'}-{suffix}"
    used.add(candidate)
    return candidate


def inspect_export(inventory: ExportInventory, previous: ImportPlan | None = None) -> ImportPlan:
    """Discover markdown candidates while preserving reviews for exact source matches."""
    previous_by_ref = {item.source_ref: item for item in previous.articles} if previous else {}
    used: set[str] = set()
    articles: list[ImportArticlePlan] = []
    for source_ref in inventory.markdown_paths:
        detected_title = _detected_title(inventory.files[source_ref].source_path, source_ref)
        old = previous_by_ref.get(source_ref)
        if old is None:
            item = ImportArticlePlan(
                source_ref=source_ref,
                detected_title=detected_title,
                include=False,
                slug=_suggest_slug(detected_title, source_ref, used),
                title=detected_title,
                published_at=None,
                kind=None,
                summary=None,
                tags=(),
            )
        else:
            slug = old.slug if old.slug not in used else _suggest_slug(old.title, source_ref, used)
            used.add(slug)
            item = ImportArticlePlan(
                source_ref=source_ref,
                detected_title=detected_title,
                include=old.include,
                slug=slug,
                title=old.title,
                published_at=old.published_at,
                kind=old.kind,
                summary=old.summary,
                tags=old.tags,
            )
        articles.append(item)
    return ImportPlan(1, "notion-export", inventory.fingerprint, tuple(articles))


def _plan_root(path: str | Path) -> Path:
    target = Path(path).resolve()
    for ancestor in (target.parent, *target.parents):
        if ancestor.name == "notion-import" and ancestor.parent.name == "build":
            try:
                target.relative_to(ancestor)
            except ValueError:
                break
            return ancestor
    raise _invalid("import plan must be below build/notion-import")


def write_import_plan(path: str | Path, plan: ImportPlan) -> None:
    """Atomically replace a plan only inside the ignored private import tree."""
    _plan_root(path)
    atomic_write_text(path, serialize_import_plan(plan))


def redact_source_ref(source_ref: str) -> str:
    """Return a normalized relative source reference with private IDs removed."""
    try:
        value = _source_ref(source_ref)
    except NotionImportError:
        return "[unsafe-source]"
    return _NOTION_ID.sub("[notion-id]", value)
