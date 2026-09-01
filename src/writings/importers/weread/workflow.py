"""Private preview and exact reviewed apply orchestration for WeRead imports."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Mapping

from shared.rendering import atomic_write_text
from shared.site_shell import render_site_page
from writings import WritingCatalogError
from writings.rendering import render_article, render_article_page

from ..durability import (
    durable_atomic_write,
    durable_remove_tree,
    durable_rename_noreplace,
    make_tree_durable,
)
from ..models import (
    PROJECT_ROOT,
    WEREAD_NAMESPACE,
    CandidateStatus,
    ExportInventory,
    ImportCandidateResult,
    ImportIssue,
    ImportRunResult,
    PreparedApplyContract,
    WeReadImportError,
    canonical_private_root,
    private_import_path,
)
from ..promoter import apply_prepared_import
from ..state import fingerprint_bundle
from .cache import SummaryCache
from .client import LoopbackChatClient
from .markdown import parse_book_notes
from .models import BookNotes, SummaryConfig, WeReadArticlePlan, WeReadPlan
from .planner import inspect_export, serialize_plan
from .rendering import render_public_bundle
from .summarizer import summarize_book


_REVIEW_VERSION = 1
_REPORT_VERSION = 1
_PREVIEW_ASSETS = (
    "assets/css/site.css",
    "assets/js/search-core.js",
    "assets/js/search.js",
    "assets/js/sidebar.js",
    "assets/js/site-shell.js",
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=False, separators=(",", ":")
    ).encode("utf-8") + b"\n"


def _issue(source_ref: str, code: str, message: str) -> ImportIssue:
    safe = WeReadImportError(code, message)
    return ImportIssue(source_ref, safe.code, safe.message)


def _candidate(
    plan: WeReadArticlePlan,
    status: CandidateStatus,
    *,
    issue: ImportIssue | None = None,
    bundle_root: Path | None = None,
    source_fingerprint: str | None = None,
    written_fingerprint: str | None = None,
) -> ImportCandidateResult:
    return ImportCandidateResult(
        plan.source_ref,
        plan.slug,
        status,
        (issue,) if issue is not None else (),
        bundle_root,
        source_fingerprint,
        written_fingerprint,
    )


def serialize_import_report(result: ImportRunResult) -> str:
    """Serialize only safe per-candidate feedback for the private WeRead report."""
    candidates: list[dict[str, object]] = []
    for candidate in result.candidates:
        issue = candidate.issues[0] if candidate.issues else None
        candidates.append(
            {
                "source_ref": candidate.source_ref,
                "slug": candidate.slug,
                "status": candidate.status,
                "code": issue.code if issue else None,
                "message": issue.message if issue else None,
            }
        )
    return _canonical_json(
        {"version": _REPORT_VERSION, "candidates": candidates}
    ).decode("utf-8")


def _validated_inputs(
    inventory: ExportInventory, plan: WeReadPlan
) -> tuple[str, Mapping[str, BookNotes]]:
    canonical_plan = serialize_plan(plan)
    if inventory.fingerprint != plan.export_fingerprint:
        raise WeReadImportError(
            "invalid_plan",
            "export fingerprint changed; rerun inspect before preview or apply",
        )

    parsed: list[tuple[BookNotes, str]] = []
    for source_path in inventory.markdown_paths:
        try:
            book = parse_book_notes(inventory.files[source_path])
        except WeReadImportError:
            continue
        identity = (
            f"book:{book.book_id}"
            if book.book_id
            else f"hash:{book.source_fingerprint}"
        )
        parsed.append((book, identity))
    counts: dict[str, int] = {}
    for _book, identity in parsed:
        counts[identity] = counts.get(identity, 0) + 1
    unique_books = [book for book, identity in parsed if counts[identity] == 1]
    inspected = inspect_export(inventory)
    if len(unique_books) != len(inspected.books):
        raise WeReadImportError(
            "invalid_plan", "export identities cannot be matched to the private plan"
        )
    resolved = {
        inspected_book.source_ref: book
        for inspected_book, book in zip(inspected.books, unique_books)
    }
    if set(resolved) != {book.source_ref for book in plan.books}:
        raise WeReadImportError(
            "invalid_plan", "plan book identities do not match the current export"
        )
    return _sha256(canonical_plan.encode("utf-8")), resolved


def _copy_preview_shell(site_root: Path) -> None:
    docs = Path(PROJECT_ROOT) / "docs"
    for relative in _PREVIEW_ASSETS:
        source = docs / relative
        if not source.is_file():
            raise WeReadImportError(
                "preview_failed", "local preview site assets are unavailable"
            )
        target = site_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _preview_index(result: ImportRunResult, site_root: Path) -> str:
    rows: list[str] = []
    for candidate in result.candidates:
        title = candidate.slug or "book"
        if candidate.status == "ready" and candidate.slug is not None:
            label = f'<a href="writings/{html.escape(candidate.slug, quote=True)}.html">{html.escape(title)}</a>'
            feedback = "Ready to apply."
        elif candidate.status == "ignored":
            label = html.escape(title)
            feedback = "Excluded by the private plan."
        else:
            label = html.escape(title)
            safe_message = (
                candidate.issues[0].message
                if candidate.issues
                else "Book is blocked."
            )
            feedback = f"{safe_message} Fix the source or local model, then run preview again."
        rows.append(
            "      <li>"
            + label
            + f' <strong>{html.escape(candidate.status)}</strong>'
            + f"<p>{html.escape(feedback)}</p></li>"
        )
    main = (
        "    <section><h1>WeChat Reading preview</h1><ul>\n"
        + "\n".join(rows)
        + "\n    </ul></section>\n"
    )
    return render_site_page(
        output_file=site_root / "index.html",
        output_root=site_root,
        active_section="writings",
        page_title="WeChat Reading preview · TOGOS",
        meta_description="Private local preview of reviewed reading summaries.",
        secondary_navigation="",
        main_content=main,
    )


def _replace_preview(stage: Path, preview: Path) -> None:
    backup = preview.with_name(f".preview-backup-{uuid.uuid4().hex}")
    moved_old = False
    try:
        make_tree_durable(stage, "weread-preview-stage")
        if os.path.lexists(preview):
            durable_rename_noreplace(preview, backup, "weread-preview-backup")
            moved_old = True
        durable_rename_noreplace(stage, preview, "weread-preview-commit")
    except BaseException:
        if moved_old and not os.path.lexists(preview) and os.path.lexists(backup):
            durable_rename_noreplace(backup, preview, "weread-preview-rollback")
        raise
    if os.path.lexists(backup):
        durable_remove_tree(backup, "weread-preview-backup-remove")


def _write_private_file(path: Path, data: bytes, label: str) -> None:
    try:
        supported = durable_atomic_write(path, data, label)
    except OSError:
        raise WeReadImportError(
            "preview_failed", "unable to write private preview metadata"
        ) from None
    if not supported:
        raise WeReadImportError(
            "preview_failed", "private preview durability is unavailable"
        )


def preview_import(
    inventory: ExportInventory,
    plan: WeReadPlan,
    model_config: SummaryConfig,
    refresh: bool = False,
) -> ImportRunResult:
    """Rebuild one private preview while isolating known per-book failures."""
    plan_fingerprint, books = _validated_inputs(inventory, plan)
    LoopbackChatClient(model_config.base_url)
    private_root = canonical_private_root(WEREAD_NAMESPACE)
    preview = private_import_path(private_root / "preview", WEREAD_NAMESPACE)
    report = Path(PROJECT_ROOT) / "build" / "reports" / WEREAD_NAMESPACE.report_name
    reviewed_path = private_import_path(
        private_root / "reviewed.json", WEREAD_NAMESPACE
    )
    stage = private_import_path(
        private_root / f".preview-stage-{uuid.uuid4().hex}", WEREAD_NAMESPACE
    )
    bundles_root = stage / "bundles"
    site_root = stage / "site"
    reviewed: list[dict[str, object]] = []
    results: list[ImportCandidateResult] = []
    cache = SummaryCache()
    try:
        bundles_root.mkdir(parents=True)
        site_root.mkdir()
        _copy_preview_shell(site_root)
        for article_plan in plan.books:
            if not article_plan.include:
                results.append(_candidate(article_plan, "ignored"))
                continue
            book = books[article_plan.source_ref]
            bundle = bundles_root / article_plan.slug
            try:
                summary = summarize_book(book, model_config, cache, refresh=refresh)
                key = cache.key_for(book, model_config.model)
                cache_bytes = cache.path_for(key).read_bytes()
                article = render_public_bundle(article_plan, book, summary, bundle)
                written_fingerprint = fingerprint_bundle(bundle)
                output_file = site_root / "writings" / f"{article_plan.slug}.html"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                rendered = render_article(
                    article, output_file=output_file, output_root=site_root
                )
                atomic_write_text(
                    output_file,
                    render_article_page(
                        article,
                        rendered,
                        output_file=output_file,
                        output_root=site_root,
                    ),
                )
                candidate = _candidate(
                    article_plan,
                    "ready",
                    bundle_root=bundle,
                    source_fingerprint=book.source_fingerprint,
                    written_fingerprint=written_fingerprint,
                )
                reviewed.append(
                    {
                        "source_ref": article_plan.source_ref,
                        "slug": article_plan.slug,
                        "source_fingerprint": book.source_fingerprint,
                        "cache_key": key.digest,
                        "cache_fingerprint": _sha256(cache_bytes),
                        "written_fingerprint": written_fingerprint,
                    }
                )
            except WeReadImportError as error:
                shutil.rmtree(bundle, ignore_errors=True)
                candidate = _candidate(
                    article_plan,
                    "blocked",
                    issue=_issue(article_plan.source_ref, error.code, error.message),
                )
            except (WritingCatalogError, ValueError, OSError):
                shutil.rmtree(bundle, ignore_errors=True)
                candidate = _candidate(
                    article_plan,
                    "blocked",
                    issue=_issue(
                        article_plan.source_ref,
                        "invalid_bundle",
                        "generated article failed strict validation",
                    ),
                )
            results.append(candidate)
        result = ImportRunResult(tuple(results))
        atomic_write_text(site_root / "index.html", _preview_index(result, site_root))
        _replace_preview(stage, preview)
        result = ImportRunResult(
            tuple(
                replace(
                    candidate,
                    bundle_root=(
                        preview / "bundles" / candidate.slug
                        if candidate.status == "ready" and candidate.slug is not None
                        else candidate.bundle_root
                    ),
                )
                for candidate in result.candidates
            )
        )
        review = {
            "version": _REVIEW_VERSION,
            "export_fingerprint": inventory.fingerprint,
            "plan_fingerprint": plan_fingerprint,
            "model": model_config.model,
            "candidates": reviewed,
        }
        _write_private_file(reviewed_path, _canonical_json(review), "weread-review-state")
        _write_private_file(
            report,
            serialize_import_report(result).encode("utf-8"),
            "weread-preview-report",
        )
        return result
    except BaseException:
        if os.path.lexists(stage):
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _load_reviewed(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("review state is too large")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise WeReadImportError(
            "missing_preview", "private preview state is unavailable; run preview again"
        ) from None
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "export_fingerprint",
            "plan_fingerprint",
            "model",
            "candidates",
        }
        or value["version"] != _REVIEW_VERSION
        or not isinstance(value["export_fingerprint"], str)
        or not isinstance(value["plan_fingerprint"], str)
        or not isinstance(value["model"], str)
        or not value["model"]
        or not isinstance(value["candidates"], list)
        or raw != _canonical_json(value)
    ):
        raise WeReadImportError(
            "missing_preview", "private preview state is invalid; run preview again"
        )
    records: dict[str, dict[str, str]] = {}
    expected = {
        "source_ref",
        "slug",
        "source_fingerprint",
        "cache_key",
        "cache_fingerprint",
        "written_fingerprint",
    }
    for item in value["candidates"]:
        if (
            not isinstance(item, dict)
            or set(item) != expected
            or any(not isinstance(item[field], str) for field in expected)
            or item["source_ref"] in records
        ):
            raise WeReadImportError(
                "missing_preview", "private preview state is invalid; run preview again"
            )
        record = {field: str(item[field]) for field in expected}
        records[record["source_ref"]] = record
    value["candidate_records"] = records
    return value


def _durable_source_ref(source_ref: str) -> str:
    _prefix, digest = source_ref.split(":", 1)
    return f"weread/{digest}.md"


def _review_changed(plan: WeReadArticlePlan) -> ImportCandidateResult:
    return _candidate(
        plan,
        "blocked",
        issue=_issue(
            plan.source_ref,
            "review_changed",
            "reviewed cache or bundle changed; run preview again",
        ),
    )


def apply_import(
    inventory: ExportInventory,
    plan: WeReadPlan,
) -> ImportRunResult:
    """Promote only exact previewed cache and bundle bytes, without a model client."""
    plan_fingerprint, books = _validated_inputs(inventory, plan)
    private_root = canonical_private_root(WEREAD_NAMESPACE)
    reviewed = _load_reviewed(
        private_import_path(private_root / "reviewed.json", WEREAD_NAMESPACE)
    )
    if (
        reviewed["export_fingerprint"] != inventory.fingerprint
        or reviewed["plan_fingerprint"] != plan_fingerprint
    ):
        raise WeReadImportError(
            "review_changed", "export or plan changed after preview; run preview again"
        )
    records = reviewed["candidate_records"]
    assert isinstance(records, dict)
    model = reviewed["model"]
    assert isinstance(model, str)
    cache = SummaryCache()
    internal_to_public = {
        _durable_source_ref(book.source_ref): book.source_ref for book in plan.books
    }

    def prepare(bundles_root: Path, _site_root: Path) -> ImportRunResult:
        candidates: list[ImportCandidateResult] = []
        for article_plan in plan.books:
            internal_ref = _durable_source_ref(article_plan.source_ref)
            internal_plan = replace(article_plan, source_ref=internal_ref)
            if not article_plan.include:
                candidates.append(_candidate(internal_plan, "ignored"))
                continue
            record = records.get(article_plan.source_ref)
            if not isinstance(record, dict) or record.get("slug") != article_plan.slug:
                candidates.append(_review_changed(internal_plan))
                continue
            book = books[article_plan.source_ref]
            bundle = bundles_root / article_plan.slug
            try:
                key = cache.key_for(book, model)
                cache_path = cache.path_for(key)
                cache_bytes = cache_path.read_bytes()
                if (
                    key.digest != record.get("cache_key")
                    or _sha256(cache_bytes) != record.get("cache_fingerprint")
                    or book.source_fingerprint != record.get("source_fingerprint")
                ):
                    raise ValueError("review binding changed")
                summary = cache.load(key)
                if summary is None:
                    raise ValueError("review cache is invalid")
                render_public_bundle(article_plan, book, summary, bundle)
                written = fingerprint_bundle(bundle)
                if written != record.get("written_fingerprint"):
                    raise ValueError("reviewed bundle changed")
                candidates.append(
                    _candidate(
                        internal_plan,
                        "ready",
                        bundle_root=bundle,
                        source_fingerprint=book.source_fingerprint,
                        written_fingerprint=written,
                    )
                )
            except (OSError, ValueError, WritingCatalogError, WeReadImportError):
                shutil.rmtree(bundle, ignore_errors=True)
                candidates.append(_review_changed(internal_plan))
        return ImportRunResult(tuple(candidates))

    contract = PreparedApplyContract(
        WEREAD_NAMESPACE,
        inventory.fingerprint,
        tuple(internal_to_public),
        prepare,
    )
    result = apply_prepared_import(
        contract,
        Path(PROJECT_ROOT) / "content" / "writings",
        private_root / "state.json",
        private_root,
        Path(PROJECT_ROOT) / "build" / "reports" / WEREAD_NAMESPACE.report_name,
    )
    return ImportRunResult(
        tuple(
            replace(
                candidate,
                source_ref=internal_to_public.get(
                    candidate.source_ref, candidate.source_ref
                ),
            )
            for candidate in result.candidates
        ),
        result.dependencies,
    )


__all__ = ["apply_import", "preview_import", "serialize_import_report"]
