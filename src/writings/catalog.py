"""Strict source-bundle discovery and safe public manifest handling."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any

import yaml

from .models import (
    CatalogResult,
    ManifestArticle,
    WritingArticle,
    WritingIssue,
    WritingManifest,
)


SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONT_MATTER_PATTERN = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)
REQUIRED_FIELDS = {"title", "slug", "published_at", "kind", "public", "summary", "tags", "source"}
SUPPORTED_KINDS = {"learning-note", "book-note"}
SUPPORTED_SOURCES = {"original", "notion", "wechat-reading"}
MANIFEST_VERSION = 1


class WritingCatalogError(ValueError):
    """Raised when the public writings catalog or manifest is unsafe."""


class _BundleValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _BundleValidationError("invalid_" + field, f"{field} must be a non-empty string")
    return value.strip()


def _parse_front_matter(content: str) -> tuple[dict[str, Any], str]:
    match = FRONT_MATTER_PATTERN.match(content)
    if match is None:
        raise _BundleValidationError("missing_front_matter", "index.md must begin with YAML front matter")
    try:
        metadata = yaml.safe_load(match.group(1))
    except (yaml.YAMLError, ValueError) as error:
        if isinstance(error, ValueError):
            raise _BundleValidationError("invalid_date", "published_at must be an ISO date") from error
        raise _BundleValidationError("invalid_front_matter", "front matter is not valid YAML") from error
    if not isinstance(metadata, dict):
        raise _BundleValidationError("invalid_front_matter", "front matter must be a mapping")
    unknown = set(metadata) - REQUIRED_FIELDS
    missing = REQUIRED_FIELDS - set(metadata)
    if unknown:
        raise _BundleValidationError("unknown_field", "front matter contains unsupported fields")
    if missing:
        raise _BundleValidationError("missing_field", "front matter is missing required fields")
    return metadata, content[match.end() :]


def _validate_article(metadata: dict[str, Any], body: str, bundle_root: Path) -> WritingArticle:
    title = _required_string(metadata["title"], "title")
    slug = _required_string(metadata["slug"], "slug")
    if not SLUG_PATTERN.fullmatch(slug):
        raise _BundleValidationError("invalid_slug", "slug must be lowercase ASCII kebab-case")
    if slug != bundle_root.name:
        raise _BundleValidationError("slug_mismatch", "slug must match the bundle directory name")

    published_value = metadata["published_at"]
    if isinstance(published_value, date):
        published_text = published_value.isoformat()
    elif isinstance(published_value, str):
        published_text = published_value
    else:
        raise _BundleValidationError("invalid_date", "published_at must be an ISO date")
    try:
        published_at = date.fromisoformat(published_text)
    except ValueError as error:
        raise _BundleValidationError("invalid_date", "published_at must be an ISO date") from error

    kind = _required_string(metadata["kind"], "kind")
    if kind not in SUPPORTED_KINDS:
        raise _BundleValidationError("invalid_kind", "kind is not supported")
    if metadata["public"] is not True:
        raise _BundleValidationError("not_public", "public must be the YAML boolean true")
    summary_value = metadata["summary"]
    if isinstance(summary_value, str) and ("\n" in summary_value or "\r" in summary_value):
        raise _BundleValidationError("invalid_summary", "summary must be one-line plain text")
    summary = _required_string(summary_value, "summary")
    if "<" in summary or ">" in summary:
        raise _BundleValidationError("invalid_summary", "summary must be one-line plain text")

    tags_value = metadata["tags"]
    if not isinstance(tags_value, list) or not tags_value:
        raise _BundleValidationError("invalid_tags", "tags must be a non-empty list")
    tags: list[str] = []
    for tag in tags_value:
        if not isinstance(tag, str) or not SLUG_PATTERN.fullmatch(tag):
            raise _BundleValidationError("invalid_tags", "tags must be lowercase kebab-case strings")
        if tag in tags:
            raise _BundleValidationError("duplicate_tag", "tags must be unique")
        tags.append(tag)

    source = _required_string(metadata["source"], "source")
    if source not in SUPPORTED_SOURCES:
        raise _BundleValidationError("invalid_source", "source is not supported")
    source_path = bundle_root / "index.md"
    return WritingArticle(
        slug=slug,
        title=title,
        published_at=published_at,
        kind=kind,
        summary=summary,
        tags=tuple(tags),
        source=source,
        source_path=source_path,
        bundle_root=bundle_root,
        body=body,
    )


def _issue_source(source_root: Path, path: Path) -> str:
    if not source_root.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        try:
            return path.relative_to(source_root).as_posix()
        except ValueError:
            return path.name


def discover_writings(source_root: Path, previous: WritingManifest) -> CatalogResult:
    """Discover independently valid public article bundles in deterministic order."""
    del previous  # Retention is applied by the publishing layer, not catalog discovery.
    root = Path(source_root)
    articles: list[WritingArticle] = []
    issues: list[WritingIssue] = []
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError as error:
        raise WritingCatalogError("Unable to read writings source root") from error
    for entry in entries:
        if entry.name == "AGENTS.md" and entry.is_file():
            continue
        issue_path = entry / "index.md" if entry.is_dir() else entry
        source = _issue_source(root, issue_path)
        if not entry.is_dir():
            issues.append(WritingIssue(source, "invalid_bundle", "source root entries must be bundle directories"))
            continue
        index_path = entry / "index.md"
        if not index_path.is_file():
            issues.append(WritingIssue(source, "missing_index", "bundle must contain index.md"))
            continue
        try:
            article = _validate_article(*_parse_front_matter(index_path.read_text(encoding="utf-8")), entry)
        except (OSError, UnicodeError) as error:
            issues.append(WritingIssue(source, "unreadable_index", "unable to read bundle index"))
        except _BundleValidationError as error:
            issues.append(WritingIssue(source, error.code, str(error)))
        else:
            articles.append(article)
    return CatalogResult(tuple(articles), tuple(issues))


def _safe_relative_posix_path(value: str, *, reject_manifest: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise WritingCatalogError("Managed path must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or path == PurePosixPath(".")
        or ".." in path.parts
        or path.as_posix() != value
        or (reject_manifest and path.name == "manifest.json")
    ):
        raise WritingCatalogError("Managed path is unsafe")
    return path


def validate_managed_path(value: str, output_root: Path) -> PurePosixPath:
    """Validate a manifest-owned path before any output-tree operation."""
    path = _safe_relative_posix_path(value, reject_manifest=True)
    root = Path(output_root).resolve()
    resolved = (root / Path(*path.parts)).resolve()
    if not resolved.is_relative_to(root):
        raise WritingCatalogError("Managed path escapes the output root")
    return path


def _manifest_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WritingCatalogError(f"Manifest {field} must be a non-empty string")
    return value


def _manifest_date(value: Any, field: str) -> str:
    text = _manifest_string(value, field)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise WritingCatalogError(f"Manifest {field} must be an ISO date") from error


def _manifest_article(slug: Any, value: Any, output_root: Path) -> ManifestArticle:
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise WritingCatalogError("Manifest article key must be a valid slug")
    if not isinstance(value, dict) or set(value) != {
        "source", "title", "published_at", "kind", "summary", "tags", "page", "assets"
    }:
        raise WritingCatalogError("Manifest article record is malformed")
    source = _manifest_string(value["source"], "article source")
    try:
        _safe_relative_posix_path(source)
    except WritingCatalogError as error:
        raise WritingCatalogError("Manifest article source must be repository-relative") from error
    title = _manifest_string(value["title"], "article title")
    published_at = _manifest_date(value["published_at"], "article published_at")
    kind = _manifest_string(value["kind"], "article kind")
    summary = _manifest_string(value["summary"], "article summary")
    tags_value = value["tags"]
    if not isinstance(tags_value, list) or any(not isinstance(tag, str) for tag in tags_value):
        raise WritingCatalogError("Manifest article tags must be a list of strings")
    if len(tags_value) != len(set(tags_value)):
        raise WritingCatalogError("Manifest article tags must be unique")
    page = validate_managed_path(value["page"], output_root).as_posix()
    assets_value = value["assets"]
    if not isinstance(assets_value, list):
        raise WritingCatalogError("Manifest article assets must be a list")
    assets = tuple(validate_managed_path(asset, output_root).as_posix() for asset in assets_value)
    if len(assets) != len(set(assets)):
        raise WritingCatalogError("Manifest article assets must be unique")
    return ManifestArticle(source, title, published_at, kind, summary, tuple(tags_value), page, assets)


def load_manifest(path: Path, output_root: Path, *, generated_on: date) -> WritingManifest:
    """Load a public manifest, refusing malformed or boundary-escaping records."""
    manifest_path = Path(path)
    root = Path(output_root).resolve()
    expected_path = root / "manifest.json"
    if manifest_path.resolve() != expected_path:
        raise WritingCatalogError("Manifest must be docs/writings/manifest.json")
    if not manifest_path.exists():
        return WritingManifest.empty(generated_on)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WritingCatalogError("Unable to load writings manifest") from error
    if not isinstance(payload, dict) or set(payload) != {"version", "generated_at", "articles", "managed_files"}:
        raise WritingCatalogError("Writings manifest is malformed")
    if type(payload["version"]) is not int or payload["version"] != MANIFEST_VERSION:
        raise WritingCatalogError("Unsupported writings manifest version")
    generated_at = _manifest_date(payload["generated_at"], "generated_at")
    articles_value = payload["articles"]
    if not isinstance(articles_value, dict):
        raise WritingCatalogError("Manifest articles must be a mapping")
    articles = {
        slug: _manifest_article(slug, record, root)
        for slug, record in articles_value.items()
    }
    managed_value = payload["managed_files"]
    if not isinstance(managed_value, list):
        raise WritingCatalogError("Manifest managed_files must be a list")
    managed_files = tuple(validate_managed_path(item, root).as_posix() for item in managed_value)
    if len(managed_files) != len(set(managed_files)):
        raise WritingCatalogError("Manifest managed_files must be unique")
    managed_set = set(managed_files)
    for article in articles.values():
        if article.page not in managed_set or not set(article.assets).issubset(managed_set):
            raise WritingCatalogError("Manifest article outputs must be managed")
    return WritingManifest(MANIFEST_VERSION, generated_at, articles, managed_files)


def serialize_manifest(manifest: WritingManifest) -> str:
    """Serialize public manifest data deterministically without local source details."""
    articles = {
        slug: {
            "source": article.source,
            "title": article.title,
            "published_at": article.published_at,
            "kind": article.kind,
            "summary": article.summary,
            "tags": list(article.tags),
            "page": article.page,
            "assets": list(article.assets),
        }
        for slug, article in sorted(manifest.articles.items())
    }
    for article in manifest.articles.values():
        try:
            _safe_relative_posix_path(article.source)
            _safe_relative_posix_path(article.page, reject_manifest=True)
            for asset in article.assets:
                _safe_relative_posix_path(asset, reject_manifest=True)
        except WritingCatalogError as error:
            raise WritingCatalogError("Manifest contains an unsafe serialized path") from error
    for path in manifest.managed_files:
        _safe_relative_posix_path(path, reject_manifest=True)
    payload = {
        "version": manifest.version,
        "generated_at": manifest.generated_at,
        "articles": articles,
        "managed_files": sorted(set(manifest.managed_files)),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
