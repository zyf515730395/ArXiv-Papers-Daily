"""Private original-draft creation and lifecycle operations."""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import shutil
import uuid

from shared.rendering import atomic_write_text
from writings.catalog import (
    SLUG_PATTERN,
    SUPPORTED_KINDS,
    WritingCatalogError,
    validate_writing_bundle,
)
from writings.importers.models import (
    PROJECT_ROOT,
    WORKBENCH_NAMESPACE,
    ImportCandidateResult,
    ImportRunResult,
    PreparedApplyContract,
    WritingImportError,
)
from writings.importers.promoter import apply_prepared_import
from writings.importers.state import fingerprint_bundle
from writings.rendering import WritingRenderError, render_article, render_article_page

from .models import OriginalResult, WorkbenchError, WorkbenchIssue
from .paths import (
    draft_root,
    original_previews_root,
    report_path,
    review_path,
    state_path,
    workbench_root,
)
from .state import load_reviews, write_reviews


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


def _draft_bundle(slug: str) -> Path:
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise WorkbenchError("invalid_slug", "slug must be lowercase ASCII kebab-case")
    bundle = draft_root() / slug
    if not bundle.is_dir():
        raise WorkbenchError("missing_draft", "private draft does not exist; run new first")
    return bundle


def _fingerprint(bundle: Path) -> str:
    try:
        return fingerprint_bundle(bundle)
    except WritingImportError as error:
        raise WorkbenchError("unsafe_draft", "private draft cannot be fingerprinted safely") from error


def _prepare_replace_tree(stage: Path, target: Path) -> Path | None:
    backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
    moved_old = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target):
            os.replace(target, backup)
            moved_old = True
        os.replace(stage, target)
        return backup if moved_old else None
    except OSError as error:
        try:
            if os.path.lexists(target):
                shutil.rmtree(target)
            if moved_old and os.path.lexists(backup):
                os.replace(backup, target)
        except OSError as rollback_error:
            raise WorkbenchError(
                "recovery_required", "private preview recovery requires attention"
            ) from rollback_error
        raise WorkbenchError("preview_failed", "unable to replace private preview safely") from error
    finally:
        if os.path.lexists(stage):
            shutil.rmtree(stage, ignore_errors=True)


def _rollback_replace_tree(target: Path, backup: Path | None) -> None:
    try:
        if os.path.lexists(target):
            shutil.rmtree(target)
        if backup is not None and os.path.lexists(backup):
            os.replace(backup, target)
    except OSError as error:
        raise WorkbenchError(
            "recovery_required", "private preview recovery requires attention"
        ) from error


def preview_original(slug: str) -> OriginalResult:
    bundle = _draft_bundle(slug)
    stage: Path | None = None
    try:
        article = validate_writing_bundle(bundle)
        if article.source != "original":
            raise WorkbenchError("invalid_source", "original draft must use source original")
        root = original_previews_root()
        target = root / slug
        stage = root / f".{slug}.stage-{uuid.uuid4().hex}"
        stage.mkdir(parents=True)
        output_file = stage / "index.html"
        rendered = render_article(article, output_file=output_file, output_root=stage)
        atomic_write_text(
            output_file,
            render_article_page(
                article, rendered, output_file=output_file, output_root=stage
            ),
        )
        source_assets = Path(PROJECT_ROOT) / "docs" / "assets"
        shutil.copytree(source_assets, stage / "assets")
        for asset in rendered.assets:
            destination = stage / Path(*Path(asset.destination).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(asset.source, destination)
        fingerprint = _fingerprint(bundle)
        backup = _prepare_replace_tree(stage, target)
        reviews = load_reviews()
        reviews[slug] = {
            "preview_fingerprint": fingerprint,
            "preview_page": f"previews/original/{slug}/index.html",
        }
        try:
            write_reviews(reviews)
        except BaseException:
            _rollback_replace_tree(target, backup)
            raise
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError as error:
                raise WorkbenchError(
                    "recovery_required", "private preview cleanup requires attention"
                ) from error
        return OriginalResult(slug, "ready")
    except WorkbenchError:
        raise
    except (WritingCatalogError, WritingRenderError) as error:
        raise WorkbenchError(
            "invalid_draft", f"draft is not previewable: {str(error)}"
        ) from error
    except OSError as error:
        raise WorkbenchError("preview_failed", "unable to build private preview") from error
    finally:
        if stage is not None and os.path.lexists(stage):
            try:
                shutil.rmtree(stage)
            except OSError as error:
                raise WorkbenchError(
                    "recovery_required", "private preview staging cleanup requires attention"
                ) from error


def _serialize_apply_report(result: ImportRunResult) -> str:
    candidates = []
    for candidate in result.candidates:
        issue = candidate.issues[0] if candidate.issues else None
        candidates.append(
            {
                "slug": candidate.slug,
                "status": candidate.status,
                "code": issue.code if issue else None,
                "message": issue.message if issue else None,
            }
        )
    return json.dumps(
        {"version": 1, "candidates": candidates},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def apply_original(slug: str) -> OriginalResult:
    bundle = _draft_bundle(slug)
    try:
        article = validate_writing_bundle(bundle)
    except WritingCatalogError as error:
        raise WorkbenchError("invalid_draft", "draft must be valid before apply") from error
    if article.source != "original":
        raise WorkbenchError("invalid_source", "original draft must use source original")
    fingerprint = _fingerprint(bundle)
    review = load_reviews().get(slug)
    if review is None or review["preview_fingerprint"] != fingerprint:
        raise WorkbenchError("preview_required", "draft changed; preview again before apply")

    source_ref = f"original/{slug}/index.md"

    def prepare(bundles: Path, _site: Path) -> ImportRunResult:
        prepared = bundles / slug
        shutil.copytree(bundle, prepared)
        return ImportRunResult(
            (
                ImportCandidateResult(
                    source_ref,
                    slug,
                    "ready",
                    (),
                    prepared,
                    fingerprint,
                    fingerprint,
                ),
            )
        )

    contract = PreparedApplyContract(
        WORKBENCH_NAMESPACE,
        fingerprint,
        (source_ref,),
        prepare,
        serialize_report=_serialize_apply_report,
    )
    try:
        result = apply_prepared_import(
            contract,
            Path(PROJECT_ROOT) / "content" / "writings",
            state_path(),
            workbench_root(),
            report_path(),
        )
    except WritingImportError as error:
        raise WorkbenchError(error.code, error.message) from error
    candidate = result.candidates[0]
    issue = (
        WorkbenchIssue(candidate.issues[0].code, candidate.issues[0].message)
        if candidate.issues
        else None
    )
    return OriginalResult(slug, candidate.status, issue)
