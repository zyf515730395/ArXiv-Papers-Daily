"""Deterministic private plans for offline Notion export inspection."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any
import unicodedata

import yaml

from shared.rendering import atomic_write_text
from writings.catalog import (
    SLUG_PATTERN,
    SUPPORTED_KINDS,
    WritingCatalogError,
    validate_managed_path,
    validate_writing_bundle,
)
from writings.rendering import WritingRenderError, render_article, render_article_page

from .models import (
    PROJECT_ROOT,
    ExportInventory,
    ImportArticlePlan,
    ImportCandidateResult,
    ImportIssue,
    ImportPlan,
    ImportRunResult,
    NotionImportError,
    portable_collision_key,
    private_import_path,
    validate_portable_relative_path,
)
from .notion_markdown import convert_notion_page


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
_REPARSE_POINT = 0x0400
_PREVIEW_ASSETS = (
    "assets/css/site.css",
    "assets/js/search-core.js",
    "assets/js/search.js",
    "assets/js/sidebar.js",
    "assets/js/site-shell.js",
)


def _invalid(message: str) -> NotionImportError:
    return NotionImportError("invalid_plan", message)


def _source_ref(value: Any) -> str:
    try:
        return validate_portable_relative_path(value).as_posix()
    except ValueError as error:
        raise _invalid("source reference must be a normalized relative POSIX path")


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
    keys = [portable_collision_key(ref) for ref in refs]
    if len(refs) != len(set(refs)) or len(keys) != len(set(keys)) or len(slugs) != len(set(slugs)):
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
        if not isinstance(key, str) or key in mapping:
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
    except NotionImportError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
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


def _detected_title(source_path: Path, source_ref: str) -> tuple[str, bool]:
    try:
        text = source_path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        text = ""
        readable = False
    except OSError as error:
        raise NotionImportError("unsafe_archive", "unable to read Markdown candidate") from error
    else:
        readable = True
    match = _H1.match(text)
    if match:
        value = unicodedata.normalize("NFKC", match.group(1)).strip().rstrip("#").strip()
        if value:
            return value, readable
    filename = unicodedata.normalize("NFKC", PurePosixPath(source_ref).stem)
    filename = _NOTION_ID.sub("", filename).strip(" -_\t")
    return " ".join(filename.split()) or "Untitled", readable


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
    used: set[str] = {item.slug for item in previous_by_ref.values() if item.source_ref in inventory.markdown_paths}
    articles: list[ImportArticlePlan] = []
    for source_ref in inventory.markdown_paths:
        detected_title, readable = _detected_title(inventory.files[source_ref].source_path, source_ref)
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
            slug = old.slug
            item = ImportArticlePlan(
                source_ref=source_ref,
                detected_title=detected_title,
                include=old.include if readable else False,
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
    try:
        return private_import_path(path).parent
    except NotionImportError as error:
        raise _invalid("import plan must be below build/notion-import") from error


def write_import_plan(path: str | Path, plan: ImportPlan) -> None:
    """Atomically replace a plan only inside the ignored private import tree."""
    target = private_import_path(path)
    try:
        atomic_write_text(target, serialize_import_plan(plan))
    except OSError as error:
        raise _invalid("unable to write import plan") from error


def redact_source_ref(source_ref: str) -> str:
    """Return a normalized relative source reference with private IDs removed."""
    try:
        value = _source_ref(source_ref)
    except NotionImportError:
        return "[unsafe-source]"
    return _NOTION_ID.sub("[notion-id]", value)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(junction and junction()) or bool(
            getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
        )
    except OSError:
        return True


def _preview_path(value: str | Path) -> Path:
    try:
        target = private_import_path(value)
    except NotionImportError as error:
        raise NotionImportError(
            "unsafe_preview", "preview root must be below build/notion-import"
        ) from error
    if target.name != "preview" or target == canonical_preview_root().parent:
        raise NotionImportError(
            "unsafe_preview", "preview root must be a dedicated preview directory"
        )
    if target.exists() and (_is_link_or_reparse(target) or not target.is_dir()):
        raise NotionImportError("unsafe_preview", "preview root is unsafe")
    return target


def canonical_preview_root() -> Path:
    return private_import_path(Path(PROJECT_ROOT) / "build" / "notion-import" / "preview")


def _report_path(value: str | Path) -> Path:
    project = Path(PROJECT_ROOT).resolve()
    expected = project / "build" / "reports" / "notion-import.json"
    lexical = Path(os.path.abspath(Path(value)))
    try:
        resolved = lexical.resolve()
    except (OSError, RuntimeError) as error:
        raise NotionImportError("unsafe_report", "import report path is unsafe") from error
    if lexical != expected or resolved != expected:
        raise NotionImportError(
            "unsafe_report", "import report must be build/reports/notion-import.json"
        )
    for component in (project / "build", expected.parent, expected):
        if component.exists() and _is_link_or_reparse(component):
            raise NotionImportError("unsafe_report", "import report path is unsafe")
    return expected


def _reset_preview(target: Path) -> None:
    try:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    except OSError as error:
        raise NotionImportError("preview_failed", "unable to rebuild local preview") from error


def _copy_preview_shell(site_root: Path) -> None:
    project = Path(PROJECT_ROOT).resolve()
    docs = project / "docs"
    for route in _PREVIEW_ASSETS:
        source = docs / Path(*PurePosixPath(route).parts)
        if _is_link_or_reparse(source) or not source.is_file():
            raise NotionImportError(
                "preview_failed", "local preview site assets are unavailable"
            )
        target = site_root / Path(*PurePosixPath(route).parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except OSError as error:
            raise NotionImportError(
                "preview_failed", "unable to copy local preview site assets"
            ) from error


def _candidate_issue(source_ref: str, error: Exception) -> ImportIssue:
    if isinstance(error, NotionImportError):
        return ImportIssue(source_ref, error.code, error.message)
    if isinstance(error, WritingRenderError):
        return ImportIssue(source_ref, error.code, str(error))
    if isinstance(error, OSError):
        return ImportIssue(
            source_ref, "preview_write_failed", "Unable to write local preview candidate"
        )
    return ImportIssue(source_ref, "invalid_bundle", str(error))


def _remove_blocked_bundle(bundle_root: Path | None, bundles_root: Path) -> None:
    if bundle_root is None or not bundle_root.exists():
        return
    try:
        resolved = bundle_root.resolve()
        if resolved.parent != bundles_root.resolve() or _is_link_or_reparse(resolved):
            raise NotionImportError("unsafe_preview", "blocked preview bundle is unsafe")
        shutil.rmtree(resolved)
    except OSError as error:
        raise NotionImportError(
            "preview_failed", "unable to remove blocked preview bundle"
        ) from error


def _remove_blocked_site(page: Path, site_root: Path, slug: str) -> None:
    writings_root = site_root / "writings"
    expected_page = writings_root / f"{slug}.html"
    assets_root = writings_root / "assets" / slug
    if page != expected_page:
        raise NotionImportError("unsafe_preview", "blocked preview page is unsafe")
    try:
        if page.exists():
            if _is_link_or_reparse(page) or not page.is_file():
                raise NotionImportError("unsafe_preview", "blocked preview page is unsafe")
            page.unlink()
        if assets_root.exists():
            if _is_link_or_reparse(assets_root) or not assets_root.is_dir():
                raise NotionImportError("unsafe_preview", "blocked preview assets are unsafe")
            shutil.rmtree(assets_root)
    except OSError as error:
        raise NotionImportError(
            "preview_failed", "unable to remove blocked preview output"
        ) from error


def _render_preview_candidate(
    article_plan: ImportArticlePlan,
    inventory: ExportInventory,
    selected_routes: dict[str, str],
    bundles_root: Path,
    site_root: Path,
) -> ImportCandidateResult:
    converted = None
    page = site_root / "writings" / f"{article_plan.slug}.html"
    try:
        converted = convert_notion_page(
            article_plan, inventory, selected_routes, bundles_root
        )
        article = validate_writing_bundle(converted.bundle_root)
        if article.slug != article_plan.slug:
            raise WritingCatalogError("validated bundle slug does not match its plan")
        rendered = render_article(article, output_file=page, output_root=site_root)
        html = render_article_page(
            article, rendered, output_file=page, output_root=site_root
        )
        asset_targets: list[tuple[Path, Path]] = []
        writings_root = site_root / "writings"
        for asset in rendered.assets:
            route = validate_managed_path(asset.destination, writings_root)
            target = writings_root / Path(*route.parts)
            asset_targets.append((asset.source, target))
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(html, encoding="utf-8", newline="\n")
        for source, target in asset_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    except (NotionImportError, WritingCatalogError, WritingRenderError, OSError) as error:
        _remove_blocked_bundle(
            converted.bundle_root if converted is not None else None, bundles_root
        )
        _remove_blocked_site(page, site_root, article_plan.slug)
        return ImportCandidateResult(
            article_plan.source_ref,
            article_plan.slug,
            "blocked",
            (_candidate_issue(article_plan.source_ref, error),),
            None,
            None,
            None,
        )
    return ImportCandidateResult(
        article_plan.source_ref,
        article_plan.slug,
        "ready",
        converted.issues,
        converted.bundle_root,
        None,
        None,
    )


def serialize_import_report(result: ImportRunResult) -> str:
    """Serialize a deterministic private report without paths, bodies, or IDs."""
    payload = {
        "version": 1,
        "counts": dict(result.counts()),
        "candidates": [
            {
                "source_ref": redact_source_ref(candidate.source_ref),
                "slug": candidate.slug,
                "status": candidate.status,
                "issues": [
                    {"code": issue.code, "message": issue.message}
                    for issue in candidate.issues
                ],
            }
            for candidate in result.candidates
        ],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=False, separators=(",", ":")
    ) + "\n"


def preview_import(
    inventory: ExportInventory,
    plan: ImportPlan,
    preview_root: str | Path,
    report_path: str | Path,
) -> ImportRunResult:
    """Rebuild a private, per-candidate-isolated preview from one validated export."""
    serialize_import_plan(plan)
    if inventory.fingerprint != plan.export_fingerprint:
        raise NotionImportError(
            "invalid_plan", "export fingerprint does not match the import plan"
        )
    preview = _preview_path(preview_root)
    report = _report_path(report_path)
    _reset_preview(preview)
    bundles_root = preview / "bundles"
    site_root = preview / "site"
    bundles_root.mkdir()
    site_root.mkdir()
    _copy_preview_shell(site_root)
    selected_routes = {
        article.source_ref: article.slug for article in plan.articles if article.include
    }
    candidates: list[ImportCandidateResult] = []
    for article in plan.articles:
        if not article.include:
            candidates.append(
                ImportCandidateResult(
                    article.source_ref,
                    article.slug,
                    "ignored",
                    (),
                    None,
                    None,
                    None,
                )
            )
            continue
        candidates.append(
            _render_preview_candidate(
                article, inventory, selected_routes, bundles_root, site_root
            )
        )
    result = ImportRunResult(tuple(candidates))
    try:
        atomic_write_text(report, serialize_import_report(result))
    except OSError as error:
        raise NotionImportError(
            "preview_failed", "unable to write private import report"
        ) from error
    return result
