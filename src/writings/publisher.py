"""Fault-tolerant writings preparation and atomic site-search publication."""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from types import MappingProxyType
from typing import Iterable

from shared.rendering import atomic_write_text
from shared.search_index import SearchDocument

from .catalog import (
    MANIFEST_VERSION,
    SLUG_PATTERN,
    WritingCatalogError,
    discover_writings,
    load_manifest,
    serialize_manifest,
    validate_managed_path,
)
from .models import (
    ManifestArticle,
    PreparedPublication,
    WritingArticle,
    WritingBuildResult,
    WritingIssue,
    WritingManifest,
)
from .rendering import (
    WritingRenderError,
    render_article,
    render_article_page,
    render_writings_index,
)


KIND_ROUTES = ("learning-note", "book-note")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")


class WritingPublishError(RuntimeError):
    """Raised when the publication as a whole cannot be committed safely."""


def _resolved(path: str | Path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError) as error:
        raise WritingPublishError("Unable to resolve a writings publication path") from error


def _validate_layout(
    source_root: str | Path, output_root: str | Path, report_path: str | Path
) -> tuple[Path, Path, Path, Path]:
    source = _resolved(source_root)
    output = _resolved(output_root)
    report = _resolved(report_path)
    if output.name != "writings" or output.parent.name != "docs":
        raise WritingPublishError("Writings output must be project-local docs/writings")
    project_root = output.parent.parent
    expected_source = _resolved(project_root / "content" / "writings")
    build_root = _resolved(project_root / "build")
    if source != expected_source or not source.is_dir():
        raise WritingPublishError("Writings source must be project-local content/writings")
    if report != _resolved(project_root / "build" / "reports" / "writings.json"):
        raise WritingPublishError("Writings report must be build/reports/writings.json")
    if not report.is_relative_to(build_root):
        raise WritingPublishError("Writings report escapes the project build root")
    if output.exists() and not output.is_dir():
        raise WritingPublishError("Writings output root must be a directory")
    return source, output, report, project_root


def _validate_output_layout(output_root: str | Path) -> tuple[Path, Path]:
    output = _resolved(output_root)
    if output.name != "writings" or output.parent.name != "docs":
        raise WritingPublishError("Writings output must be project-local docs/writings")
    if output.exists() and not output.is_dir():
        raise WritingPublishError("Writings output root must be a directory")
    return output, output.parent


def _safe_remove_tree(path: Path, allowed_parent: Path) -> None:
    resolved = _resolved(path)
    parent = _resolved(allowed_parent)
    if resolved.parent != parent or not resolved.name.startswith(".writings-"):
        raise WritingPublishError("Refusing to remove an unsafe publication work directory")
    if resolved.exists():
        shutil.rmtree(resolved)


def _new_work_directory(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    created = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    resolved = _resolved(created)
    if resolved.parent != _resolved(parent) or not resolved.name.startswith(prefix):
        raise WritingPublishError("Publication work directory escaped its allowed parent")
    return resolved


def _write_stage_text(staging_root: Path, relative_path: str, content: str) -> None:
    destination = staging_root / Path(*PurePosixPath(relative_path).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _copy_stage_file(source: Path, staging_root: Path, relative_path: str) -> None:
    destination = staging_root / Path(*PurePosixPath(relative_path).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _replace_from_sibling_copy(source: Path, destination: Path) -> None:
    """Atomically replace a target with bytes created under its parent ACL."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.writings-promote-",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists() and temporary.is_file():
            temporary.unlink()


def _article_source(article: WritingArticle, project_root: Path) -> str:
    try:
        relative = article.source_path.resolve().relative_to(project_root)
    except (OSError, ValueError) as error:
        raise WritingPublishError("Article source escapes the project root") from error
    return relative.as_posix()


def _issue_bundle(issue: WritingIssue, bundle_names: Iterable[str]) -> str | None:
    normalized = issue.source.replace("\\", "/").strip("/")
    for name in sorted(bundle_names, key=len, reverse=True):
        if normalized == name or normalized.startswith(name + "/"):
            return name
        marker = f"/writings/{name}/"
        if marker in "/" + normalized + "/":
            return name
    return None


def _sanitize_text(value: str, fallback: str) -> str:
    text = CONTROL_CHARACTERS.sub(" ", str(value)).strip()
    return (text or fallback)[:240]


def _safe_issue(issue: WritingIssue, bundle: str | None) -> WritingIssue:
    if bundle and SLUG_PATTERN.fullmatch(bundle):
        source = f"content/writings/{bundle}/index.md"
    else:
        source = "content/writings/[invalid-bundle]"
    return WritingIssue(
        source=source,
        code=_sanitize_text(issue.code, "article_build_failed"),
        message=_sanitize_text(issue.message, "Unable to publish article"),
    )


def _article_issue(article: WritingArticle, error: Exception) -> WritingIssue:
    if isinstance(error, WritingRenderError):
        code = error.code
        message = str(error)
    else:
        code = "article_build_failed"
        message = "Unable to publish article"
    return WritingIssue(
        f"content/writings/{article.slug}/index.md",
        _sanitize_text(code, "article_build_failed"),
        _sanitize_text(message, "Unable to publish article"),
    )


def _validate_article_routes(records: dict[str, ManifestArticle]) -> None:
    owners: dict[str, str] = {}
    for slug, record in sorted(records.items()):
        if not SLUG_PATTERN.fullmatch(slug):
            raise WritingPublishError("Article output route has an invalid owner")
        routes = (record.page, *record.assets)
        if len(routes) != len(set(routes)):
            raise WritingPublishError("Article output routes must be unique")
        for route in routes:
            if route in owners:
                raise WritingPublishError("Duplicate article output route")
            owners[route] = slug


def _retain_article(
    slug: str,
    previous: WritingManifest,
    output_root: Path,
    staging_root: Path,
) -> ManifestArticle:
    record = previous.articles[slug]
    for relative_path in (record.page, *record.assets):
        validated = validate_managed_path(relative_path, output_root).as_posix()
        source = output_root / Path(*PurePosixPath(validated).parts)
        if not source.is_file():
            raise WritingPublishError("A file required to retain a public article is missing")
        _copy_stage_file(source, staging_root, validated)
    return record


def _render_article_to_stage(
    article: WritingArticle,
    output_root: Path,
    staging_root: Path,
    project_root: Path,
) -> ManifestArticle:
    page = f"{article.slug}.html"
    final_page = output_root / page
    rendered = render_article(
        article,
        output_file=final_page,
        output_root=output_root.parent,
    )
    _write_stage_text(
        staging_root,
        page,
        render_article_page(
            article,
            rendered,
            output_file=final_page,
            output_root=output_root.parent,
        ),
    )
    asset_routes: list[str] = []
    for asset in rendered.assets:
        route = validate_managed_path(asset.destination, output_root).as_posix()
        _copy_stage_file(asset.source, staging_root, route)
        asset_routes.append(route)
    return ManifestArticle(
        source=_article_source(article, project_root),
        title=article.title,
        published_at=article.published_at.isoformat(),
        kind=article.kind,
        summary=article.summary,
        tags=article.tags,
        page=page,
        assets=tuple(asset_routes),
    )


def _render_listing_pages(
    records: dict[str, ManifestArticle], output_root: Path, staging_root: Path
) -> tuple[str, ...]:
    site_root = output_root.parent
    routes: list[tuple[str, tuple[str, str] | None]] = [
        ("index.html", None),
        *((f"kind/{kind}.html", ("kind", kind)) for kind in KIND_ROUTES),
    ]
    tags = sorted({tag for record in records.values() for tag in record.tags})
    routes.extend((f"tag/{tag}.html", ("tag", tag)) for tag in tags)
    article_routes = {
        route
        for record in records.values()
        for route in (record.page, *record.assets)
    }
    generated_routes = {route for route, _ in routes}
    if article_routes & generated_routes:
        raise WritingPublishError("Article output route collides with a generated listing")
    for route, active_filter in routes:
        final_page = output_root / Path(*PurePosixPath(route).parts)
        _write_stage_text(
            staging_root,
            route,
            render_writings_index(
                records,
                active_filter=active_filter,
                output_file=final_page,
                output_root=site_root,
            ),
        )
    return tuple(route for route, _ in routes)


def _search_documents(records: dict[str, ManifestArticle]) -> tuple[SearchDocument, ...]:
    return tuple(
        SearchDocument(
            id=f"article:{slug}",
            title=record.title,
            url=f"writings/{record.page}",
            section="writings",
            kind="article",
            published_at=record.published_at,
        )
        for slug, record in sorted(records.items())
    )


def _write_report(path: Path, generated_on: date, result: WritingBuildResult) -> None:
    status = "degraded" if result.issues else "ok"
    payload = {
        "version": 1,
        "generated_at": generated_on.isoformat(),
        "status": status,
        "counts": {
            "published": len(result.published),
            "retained": len(result.retained),
            "skipped": len(result.skipped),
            "removed": len(result.removed),
            "issues": len(result.issues),
        },
        "published": list(result.published),
        "retained": list(result.retained),
        "skipped": list(result.skipped),
        "removed": list(result.removed),
        "issues": [
            {"source": issue.source, "code": issue.code, "message": issue.message}
            for issue in result.issues
        ],
    }
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    )


def prepare_writings_publication(
    source_root: str | Path,
    output_root: str | Path,
    report_path: str | Path,
    generated_on: date,
) -> PreparedPublication:
    """Prepare all writings in a sibling staging tree without mutating public output."""
    if not isinstance(generated_on, date):
        raise WritingPublishError("generated_on must be a date")
    source, output, report, project_root = _validate_layout(
        source_root, output_root, report_path
    )
    try:
        previous = load_manifest(
            output / "manifest.json", output, generated_on=generated_on
        )
        _validate_article_routes(dict(previous.articles))
    except WritingCatalogError as error:
        raise WritingPublishError("Unable to load the previous writings manifest") from error

    staging = _new_work_directory(output.parent, ".writings-staging-")
    try:
        entries = {
            entry.name
            for entry in source.iterdir()
            if entry.name != "AGENTS.md"
        }
        catalog = discover_writings(source, previous)
        records: dict[str, ManifestArticle] = {}
        published: list[str] = []
        retained: list[str] = []
        skipped: set[str] = set()
        issues: list[WritingIssue] = []

        for issue in catalog.issues:
            bundle = _issue_bundle(issue, entries)
            safe_issue = _safe_issue(issue, bundle)
            issues.append(safe_issue)
            if bundle is None:
                continue
            if bundle in previous.articles:
                records[bundle] = _retain_article(bundle, previous, output, staging)
                retained.append(bundle)
            elif SLUG_PATTERN.fullmatch(bundle):
                skipped.add(bundle)

        for article in catalog.articles:
            try:
                record = _render_article_to_stage(
                    article, output, staging, project_root
                )
            except WritingPublishError:
                raise
            except Exception as error:
                issues.append(_article_issue(article, error))
                if article.slug in previous.articles:
                    records[article.slug] = _retain_article(
                        article.slug, previous, output, staging
                    )
                    retained.append(article.slug)
                else:
                    skipped.add(article.slug)
            else:
                records[article.slug] = record
                published.append(article.slug)

        records = dict(sorted(records.items()))
        _validate_article_routes(records)
        listing_routes = _render_listing_pages(records, output, staging)
        managed = sorted(
            {
                *listing_routes,
                *(
                    route
                    for record in records.values()
                    for route in (record.page, *record.assets)
                ),
            }
        )
        for route in managed:
            validate_managed_path(route, output)
        next_manifest = WritingManifest(
            version=MANIFEST_VERSION,
            generated_at=generated_on.isoformat(),
            articles=MappingProxyType(records),
            managed_files=tuple(managed),
        )
        _write_stage_text(staging, "manifest.json", serialize_manifest(next_manifest))
        removed = tuple(sorted(set(previous.articles) - set(records)))
        result = WritingBuildResult(
            published=tuple(sorted(published)),
            retained=tuple(sorted(retained)),
            skipped=tuple(sorted(skipped)),
            removed=removed,
            issues=tuple(sorted(issues, key=lambda item: (item.source, item.code, item.message))),
            search_documents=_search_documents(records),
        )
        _write_report(report, generated_on, result)
        return PreparedPublication(
            staging_root=staging,
            output_root=output,
            previous_manifest=previous,
            next_manifest=next_manifest,
            result=result,
        )
    except Exception as error:
        _safe_remove_tree(staging, output.parent)
        if isinstance(error, WritingPublishError):
            raise
        if isinstance(error, WritingCatalogError):
            raise WritingPublishError("Writings publication contains an unsafe path") from error
        raise WritingPublishError("Unable to prepare writings publication") from error


def _validated_managed_files(manifest: WritingManifest, output_root: Path) -> tuple[str, ...]:
    validated = tuple(
        validate_managed_path(path, output_root).as_posix()
        for path in manifest.managed_files
    )
    if len(validated) != len(set(validated)):
        raise WritingPublishError("Manifest managed paths must be unique")
    return validated


def _remove_empty_managed_parents(paths: Iterable[Path], stop: Path) -> None:
    candidates = sorted(
        {parent for path in paths for parent in path.parents if parent != stop and parent.is_relative_to(stop)},
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in candidates:
        try:
            directory.rmdir()
        except OSError:
            pass


def commit_writings_and_search(
    prepared: PreparedPublication,
    search_index_path: str | Path,
    search_content: str,
) -> None:
    """Commit writings and the global search index as one rollback-safe transaction."""
    output, site_root = _validate_output_layout(prepared.output_root)
    staging = _resolved(prepared.staging_root)
    search_path = _resolved(search_index_path)
    if staging.parent != site_root or not staging.name.startswith(".writings-staging-"):
        raise WritingPublishError("Writings staging root is outside the site root")
    if not staging.is_dir():
        raise WritingPublishError("Writings staging root is missing")
    try:
        if search_path != site_root / "search-index.json":
            raise WritingPublishError("Search index must be docs/search-index.json")
        if not isinstance(search_content, str):
            raise WritingPublishError("Search index content must be text")
        try:
            payload = json.loads(search_content)
        except json.JSONDecodeError as error:
            raise WritingPublishError("Prepared search index is not valid JSON") from error
        if not isinstance(payload, dict):
            raise WritingPublishError("Prepared search index is malformed")
        try:
            previous_files = _validated_managed_files(prepared.previous_manifest, output)
            next_files = _validated_managed_files(prepared.next_manifest, output)
            _validate_article_routes(dict(prepared.previous_manifest.articles))
            _validate_article_routes(dict(prepared.next_manifest.articles))
            for relative in (*next_files, "manifest.json"):
                stage_file = staging / Path(*PurePosixPath(relative).parts)
                if not stage_file.is_file():
                    raise WritingPublishError("Prepared publication is missing a staged file")
        except WritingCatalogError as error:
            raise WritingPublishError("Prepared publication contains an unsafe managed path") from error
        output.mkdir(parents=True, exist_ok=True)
        backup = _new_work_directory(site_root, ".writings-backup-")
    except Exception:
        _safe_remove_tree(staging, site_root)
        raise

    prepared_search = site_root / f".search-index.prepared-{os.getpid()}-{backup.name.rsplit('-', 1)[-1]}"
    mutation_targets: list[Path] = []
    backups: dict[Path, Path | None] = {}
    try:
        atomic_write_text(prepared_search, search_content)
        managed_union = sorted(set(previous_files) | set(next_files))
        mutation_targets.extend(
            output / Path(*PurePosixPath(relative).parts)
            for relative in managed_union
        )
        mutation_targets.extend((output / "manifest.json", search_path))
        for target in mutation_targets:
            relative = target.relative_to(site_root)
            backup_file = backup / relative
            if target.exists():
                if not target.is_file():
                    raise WritingPublishError("A publication target is not a regular file")
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_file)
                backups[target] = backup_file
            else:
                backups[target] = None

        for relative in sorted(next_files):
            source = staging / Path(*PurePosixPath(relative).parts)
            destination = output / Path(*PurePosixPath(relative).parts)
            _replace_from_sibling_copy(source, destination)
        _replace_from_sibling_copy(
            staging / "manifest.json", output / "manifest.json"
        )
        _replace_from_sibling_copy(prepared_search, search_path)
        for relative in sorted(set(previous_files) - set(next_files)):
            stale = output / Path(*PurePosixPath(relative).parts)
            if stale.exists():
                if not stale.is_file():
                    raise WritingPublishError("A stale managed path is not a regular file")
                stale.unlink()
        _remove_empty_managed_parents(
            (
                output / Path(*PurePosixPath(relative).parts)
                for relative in set(previous_files) - set(next_files)
            ),
            output,
        )
    except Exception as error:
        rollback_errors: list[Exception] = []
        for target in reversed(tuple(backups)):
            backup_file = backups[target]
            try:
                if backup_file is None:
                    if target.exists() and target.is_file():
                        target.unlink()
                elif backup_file.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_file, target)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        _remove_empty_managed_parents(
            (target for target, backup_file in backups.items() if backup_file is None),
            site_root,
        )
        if rollback_errors:
            raise WritingPublishError("Writings transaction failed and rollback was incomplete") from error
        raise WritingPublishError("Writings and search publication transaction failed") from error
    finally:
        if prepared_search.exists() and prepared_search.is_file():
            prepared_search.unlink()
        _safe_remove_tree(staging, site_root)
        _safe_remove_tree(backup, site_root)
