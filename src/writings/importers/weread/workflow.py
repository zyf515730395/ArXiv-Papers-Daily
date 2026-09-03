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
from shared.site_shell import SITE_NAME, render_context_strip, render_site_page
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
    NotionImportError,
    PreparedApplyContract,
    WeReadImportError,
    canonical_private_root,
    private_import_path,
)
from ..promoter import apply_prepared_import, validate_exact_report_path
from ..state import fingerprint_bundle
from .cache import SummaryCache
from .client import LoopbackChatClient
from .feedback import remediation_for
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
                "preview_assets_unavailable",
                "local preview site assets are unavailable",
            )
        target = site_root / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError:
            raise WeReadImportError(
                "preview_assets_unavailable",
                "local preview site assets cannot be copied",
            ) from None


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
            code = candidate.issues[0].code if candidate.issues else "import_failed"
            feedback = f"{safe_message.rstrip('.')}. {remediation_for(code)}"
        rows.append(
            "      <li>"
            + label
            + f' <strong>{html.escape(candidate.status)}</strong>'
            + f"<p>{html.escape(feedback)}</p></li>"
        )
    main = (
        "    <section><h1>WeChat Reading preview</h1>\n"
        + render_context_strip((("PREVIEW", "#top"),))
        + "<ul>\n"
        + "\n".join(rows)
        + "\n    </ul></section>\n"
    )
    return render_site_page(
        output_file=site_root / "index.html",
        output_root=site_root,
        active_section="writings",
        page_title=f"WeChat Reading preview · {SITE_NAME}",
        meta_description="Private local preview of reviewed reading summaries.",
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


def _write_private_file(
    path: Path, data: bytes, label: str, *, code: str, message: str
) -> None:
    try:
        supported = durable_atomic_write(path, data, label)
    except OSError:
        raise WeReadImportError(
            code, message
        ) from None
    if not supported:
        raise WeReadImportError(
            code, message
        )


def preview_import(
    inventory: ExportInventory,
    plan: WeReadPlan,
    model_config: SummaryConfig,
    refresh: bool = False,
) -> ImportRunResult:
    """Rebuild one private preview while isolating known per-book failures."""
    plan_fingerprint, books = _validated_inputs(inventory, plan)
    report = validate_exact_report_path(
        WEREAD_NAMESPACE,
        Path(PROJECT_ROOT) / "build" / "reports" / WEREAD_NAMESPACE.report_name,
    )
    LoopbackChatClient(model_config.base_url)
    private_root = canonical_private_root(WEREAD_NAMESPACE)
    preview = private_import_path(private_root / "preview", WEREAD_NAMESPACE)
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
        try:
            bundles_root.mkdir(parents=True)
            site_root.mkdir()
        except OSError:
            raise WeReadImportError(
                "preview_stage_failed", "unable to create the private preview stage"
            ) from None
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
                try:
                    cache_bytes = cache.path_for(key).read_bytes()
                except OSError:
                    raise WeReadImportError(
                        "cache_read_failed",
                        "unable to bind the reviewed private summary cache",
                    ) from None
                try:
                    article = render_public_bundle(
                        article_plan, book, summary, bundle
                    )
                except OSError:
                    raise WeReadImportError(
                        "bundle_write_failed",
                        "unable to write the generated article bundle",
                    ) from None
                except (WritingCatalogError, ValueError):
                    raise WeReadImportError(
                        "bundle_invalid",
                        "generated article failed strict validation",
                    ) from None
                try:
                    written_fingerprint = fingerprint_bundle(bundle)
                except (NotionImportError, OSError):
                    raise WeReadImportError(
                        "bundle_verify_failed",
                        "generated article bundle cannot be verified",
                    ) from None
                output_file = site_root / "writings" / f"{article_plan.slug}.html"
                try:
                    rendered = render_article(
                        article, output_file=output_file, output_root=site_root
                    )
                    page = render_article_page(
                        article,
                        rendered,
                        output_file=output_file,
                        output_root=site_root,
                    )
                except (WritingCatalogError, ValueError, OSError):
                    raise WeReadImportError(
                        "preview_render_failed",
                        "generated article preview cannot be rendered",
                    ) from None
                try:
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(output_file, page)
                except OSError:
                    raise WeReadImportError(
                        "preview_write_failed",
                        "generated article preview cannot be written",
                    ) from None
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
            results.append(candidate)
        result = ImportRunResult(tuple(results))
        try:
            index = _preview_index(result, site_root)
        except (WritingCatalogError, ValueError, OSError):
            raise WeReadImportError(
                "preview_render_failed", "private preview index cannot be rendered"
            ) from None
        try:
            atomic_write_text(site_root / "index.html", index)
        except OSError:
            raise WeReadImportError(
                "preview_write_failed", "private preview index cannot be written"
            ) from None
        try:
            _replace_preview(stage, preview)
        except OSError:
            raise WeReadImportError(
                "preview_swap_failed", "private preview cannot be committed"
            ) from None
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
        _write_private_file(
            reviewed_path,
            _canonical_json(review),
            "weread-review-state",
            code="review_state_write_failed",
            message="unable to write private review state",
        )
        _write_private_file(
            report,
            serialize_import_report(result).encode("utf-8"),
            "weread-preview-report",
            code="report_write_failed",
            message="unable to write private import report",
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


def _blocked_phase(
    plan: WeReadArticlePlan, code: str, message: str
) -> ImportCandidateResult:
    return _candidate(
        plan,
        "blocked",
        issue=_issue(plan.source_ref, code, message),
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
            except (OSError, ValueError, WeReadImportError):
                shutil.rmtree(bundle, ignore_errors=True)
                candidates.append(
                    _blocked_phase(
                        internal_plan,
                        "cache_read_failed",
                        "reviewed private summary cache cannot be read",
                    )
                )
                continue
            if (
                key.digest != record.get("cache_key")
                or _sha256(cache_bytes) != record.get("cache_fingerprint")
                or book.source_fingerprint != record.get("source_fingerprint")
            ):
                candidates.append(
                    _blocked_phase(
                        internal_plan,
                        "cache_changed",
                        "reviewed private summary cache changed; run preview again",
                    )
                )
                continue
            summary = cache.load(key)
            if summary is None:
                candidates.append(
                    _blocked_phase(
                        internal_plan,
                        "cache_changed",
                        "reviewed private summary cache changed; run preview again",
                    )
                )
                continue
            try:
                render_public_bundle(article_plan, book, summary, bundle)
            except WeReadImportError as error:
                shutil.rmtree(bundle, ignore_errors=True)
                candidates.append(
                    _blocked_phase(internal_plan, error.code, error.message)
                )
                continue
            except OSError:
                shutil.rmtree(bundle, ignore_errors=True)
                candidates.append(
                    _blocked_phase(
                        internal_plan,
                        "bundle_write_failed",
                        "reviewed article bundle cannot be written",
                    )
                )
                continue
            except (ValueError, WritingCatalogError):
                shutil.rmtree(bundle, ignore_errors=True)
                candidates.append(
                    _blocked_phase(
                        internal_plan,
                        "bundle_invalid",
                        "reviewed article bundle failed strict validation",
                    )
                )
                continue
            try:
                written = fingerprint_bundle(bundle)
            except (NotionImportError, OSError):
                shutil.rmtree(bundle, ignore_errors=True)
                candidates.append(
                    _blocked_phase(
                        internal_plan,
                        "bundle_verify_failed",
                        "reviewed article bundle cannot be verified",
                    )
                )
                continue
            if written != record.get("written_fingerprint"):
                shutil.rmtree(bundle, ignore_errors=True)
                candidates.append(_review_changed(internal_plan))
                continue
            candidates.append(
                _candidate(
                    internal_plan,
                    "ready",
                    bundle_root=bundle,
                    source_fingerprint=book.source_fingerprint,
                    written_fingerprint=written,
                )
            )
        return ImportRunResult(tuple(candidates))

    def project_result(result: ImportRunResult) -> ImportRunResult:
        return ImportRunResult(
            tuple(
                replace(
                    candidate,
                    source_ref=internal_to_public.get(
                        candidate.source_ref, candidate.source_ref
                    ),
                    issues=tuple(
                        replace(
                            issue,
                            source=internal_to_public.get(issue.source, issue.source),
                        )
                        for issue in candidate.issues
                    ),
                )
                for candidate in result.candidates
            ),
            result.dependencies,
        )

    contract = PreparedApplyContract(
        WEREAD_NAMESPACE,
        inventory.fingerprint,
        tuple(internal_to_public),
        prepare,
        project_result=project_result,
        serialize_report=serialize_import_report,
    )
    return apply_prepared_import(
        contract,
        Path(PROJECT_ROOT) / "content" / "writings",
        private_root / "state.json",
        private_root,
        Path(PROJECT_ROOT) / "build" / "reports" / WEREAD_NAMESPACE.report_name,
    )


__all__ = ["apply_import", "preview_import", "serialize_import_report"]
