"""Guarded, per-article promotion of rebuilt Notion writing bundles."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil

from shared.rendering import atomic_write_text

from .models import (
    CandidateStatus,
    ExportInventory,
    ImportCandidateResult,
    ImportIssue,
    ImportPlan,
    ImportRunResult,
    ImportState,
    ImportStateEntry,
    NotionImportError,
    private_import_path,
)
from .planner import (
    prepare_import_candidates,
    serialize_import_plan,
    serialize_import_report,
)
from .state import (
    fingerprint_bundle,
    load_import_state,
    source_key,
    write_import_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REPARSE_POINT = 0x0400
_STAGE_PREFIX = ".notion-import-stage-"
_BACKUP_PREFIX = ".notion-import-backup-"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(junction and junction()) or bool(
            getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
        )
    except OSError:
        return True


def _global(code: str, message: str) -> NotionImportError:
    return NotionImportError(code, message)


def _exact_path(value: str | Path, expected: Path, code: str) -> Path:
    lexical = Path(os.path.abspath(Path(value)))
    if lexical != expected:
        raise _global(code, "import path does not match the canonical project root")
    for component in (Path(PROJECT_ROOT), *expected.parents[::-1], expected):
        if os.path.lexists(component) and _is_link_or_reparse(component):
            raise _global(code, "import path contains a link or reparse point")
    try:
        resolved_project = Path(PROJECT_ROOT).resolve()
        resolved = lexical.resolve()
        resolved_expected = expected.resolve()
    except (OSError, RuntimeError) as error:
        raise _global(code, "import path is unsafe") from error
    if not resolved.is_relative_to(resolved_project) or resolved != resolved_expected:
        raise _global(code, "import path escapes the canonical project root")
    return lexical


def _canonical_paths(
    content_root: str | Path,
    state_path: str | Path,
    work_root: str | Path,
    report_path: str | Path,
) -> tuple[Path, Path, Path, Path]:
    project = Path(PROJECT_ROOT)
    content = _exact_path(content_root, project / "content" / "writings", "unsafe_root")
    state = _exact_path(
        state_path, project / "build" / "notion-import" / "state.json", "invalid_state"
    )
    work = _exact_path(work_root, project / "build" / "notion-import", "unsafe_root")
    report = _exact_path(
        report_path, project / "build" / "reports" / "notion-import.json", "unsafe_report"
    )
    try:
        content.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _global("unsafe_root", "unable to create canonical import directories") from error
    return content, state, work, report


def _detect_residue(content_root: Path) -> None:
    try:
        residue = [
            child
            for child in content_root.iterdir()
            if child.name.startswith((_STAGE_PREFIX, _BACKUP_PREFIX))
        ]
    except OSError as error:
        raise _global("unsafe_root", "unable to inspect writing source root") from error
    if residue:
        raise _global(
            "recovery_required",
            "import recovery residue requires human inspection before apply",
        )


def _reset_apply_workspace(work_root: Path) -> tuple[Path, Path, Path]:
    apply_root = private_import_path(work_root / "apply")
    if os.path.lexists(apply_root):
        if _is_link_or_reparse(apply_root) or not apply_root.is_dir():
            raise _global("unsafe_root", "apply workspace is unsafe")
        try:
            shutil.rmtree(apply_root)
        except OSError as error:
            raise _global("unsafe_root", "unable to reset apply workspace") from error
    bundles = apply_root / "bundles"
    site = apply_root / "site"
    try:
        bundles.mkdir(parents=True)
        site.mkdir()
    except OSError as error:
        raise _global("unsafe_root", "unable to create apply workspace") from error
    return apply_root, bundles, site


def _write_report(report_path: Path, candidates: list[ImportCandidateResult]) -> None:
    try:
        atomic_write_text(
            report_path, serialize_import_report(ImportRunResult(tuple(candidates)))
        )
    except OSError as error:
        raise _global("promotion_failed", "unable to refresh private import report") from error


def _result(
    candidate: ImportCandidateResult,
    status: CandidateStatus,
    *,
    issue: ImportIssue | None = None,
    source_fingerprint: str | None = None,
    written_fingerprint: str | None = None,
) -> ImportCandidateResult:
    issues = candidate.issues + ((issue,) if issue else ())
    return ImportCandidateResult(
        candidate.source_ref,
        candidate.slug,
        status,
        issues,
        None,
        source_fingerprint,
        written_fingerprint,
    )


def _unique_sibling(content_root: Path, prefix: str, slug: str) -> Path:
    for _ in range(32):
        candidate = content_root / f"{prefix}{slug}-{secrets.token_hex(8)}"
        if not os.path.lexists(candidate):
            return candidate
    raise _global("promotion_failed", "unable to allocate a unique promotion path")


def _copy_stage(source: Path, content_root: Path, slug: str, expected: str) -> Path:
    stage = _unique_sibling(content_root, _STAGE_PREFIX, slug)
    try:
        shutil.copytree(source, stage, symlinks=False)
        if fingerprint_bundle(stage) != expected:
            raise OSError("staged bundle changed while copying")
    except (OSError, NotionImportError) as error:
        try:
            if os.path.lexists(stage):
                if _is_link_or_reparse(stage) or not stage.is_dir():
                    raise OSError("failed stage is unsafe")
                shutil.rmtree(stage)
        except OSError as cleanup_error:
            raise _global(
                "recovery_failed", "unable to prove failed stage cleanup"
            ) from cleanup_error
        if isinstance(error, NotionImportError):
            raise
        raise _global("promotion_failed", "unable to stage writing bundle") from error
    return stage


def _verified_tree(path: Path, parent: Path, expected: str) -> None:
    try:
        lexical = Path(os.path.abspath(path))
        resolved = path.resolve()
        resolved_parent = parent.resolve()
    except (OSError, RuntimeError) as error:
        raise _global("recovery_failed", "unable to verify promotion residue") from error
    if (
        lexical.parent != parent
        or resolved.parent != resolved_parent
        or _is_link_or_reparse(path)
        or not path.is_dir()
        or fingerprint_bundle(path) != expected
    ):
        raise _global("recovery_failed", "promotion residue cannot be verified")


def _remove_verified_tree(path: Path, parent: Path, expected: str) -> None:
    _verified_tree(path, parent, expected)
    try:
        shutil.rmtree(path)
    except OSError as error:
        raise _global("recovery_failed", "unable to remove verified promotion target") from error
    if os.path.lexists(path):
        raise _global("recovery_failed", "promotion target removal cannot be proven")


def _rollback(
    content_root: Path,
    target: Path,
    backup: Path | None,
    candidate_fingerprint: str,
    previous_fingerprint: str | None,
) -> None:
    _remove_verified_tree(target, content_root, candidate_fingerprint)
    if backup is None:
        return
    assert previous_fingerprint is not None
    _verified_tree(backup, content_root, previous_fingerprint)
    try:
        os.replace(backup, target)
    except OSError as error:
        raise _global("recovery_failed", "unable to restore verified writing backup") from error
    _verified_tree(target, content_root, previous_fingerprint)


def _conflict(candidate: ImportCandidateResult, message: str) -> ImportCandidateResult:
    return _result(
        candidate,
        "conflict",
        issue=ImportIssue(candidate.source_ref, "bundle_conflict", message),
    )


def _cleanup_apply_workspace(apply_root: Path) -> None:
    if not os.path.lexists(apply_root):
        return
    if _is_link_or_reparse(apply_root) or not apply_root.is_dir():
        raise _global("promotion_failed", "private apply workspace is unsafe")
    try:
        shutil.rmtree(apply_root)
    except OSError as error:
        raise _global(
            "promotion_failed", "unable to clean private apply workspace"
        ) from error
    if os.path.lexists(apply_root):
        raise _global(
            "promotion_failed", "private apply workspace cleanup is incomplete"
        )


def _promote_candidate(
    candidate: ImportCandidateResult,
    inventory: ExportInventory,
    content_root: Path,
    state_path: Path,
    state: ImportState,
) -> tuple[ImportCandidateResult, ImportState]:
    assert candidate.slug is not None and candidate.bundle_root is not None
    key = source_key(candidate.source_ref)
    entry = state.sources.get(key)
    owners = [item for item in state.sources.values() if item.slug == candidate.slug]
    target = content_root / candidate.slug
    source_fingerprint = "sha256:" + inventory.files[candidate.source_ref].sha256
    candidate_fingerprint = fingerprint_bundle(candidate.bundle_root)
    target_exists = os.path.lexists(target)
    if entry is not None and entry.slug != candidate.slug:
        return _conflict(candidate, "Private state owns a different public slug"), state
    if owners and (entry is None or owners != [entry]):
        return _conflict(candidate, "Public slug is owned by another import source"), state
    previous_fingerprint: str | None = None
    if target_exists:
        if entry is None or len(owners) != 1:
            return _conflict(candidate, "Existing writing bundle has no trusted state"), state
        try:
            previous_fingerprint = fingerprint_bundle(target)
        except NotionImportError:
            return _conflict(candidate, "Existing writing bundle is unsafe"), state
        if previous_fingerprint != entry.written_fingerprint:
            return _conflict(candidate, "Existing writing bundle contains human edits"), state
        if candidate_fingerprint == previous_fingerprint:
            return (
                _result(
                    candidate,
                    "unchanged",
                    source_fingerprint=source_fingerprint,
                    written_fingerprint=candidate_fingerprint,
                ),
                state,
            )
    elif entry is not None or owners:
        return _conflict(candidate, "Private state and public bundle disagree"), state

    stage = _copy_stage(
        candidate.bundle_root, content_root, candidate.slug, candidate_fingerprint
    )
    backup: Path | None = None
    backup_moved = False
    promoted = False
    try:
        if target_exists:
            assert previous_fingerprint is not None
            backup = _unique_sibling(content_root, _BACKUP_PREFIX, candidate.slug)
            os.replace(target, backup)
            backup_moved = True
            _verified_tree(backup, content_root, previous_fingerprint)
        os.replace(stage, target)
        promoted = True
        _verified_tree(target, content_root, candidate_fingerprint)
    except (OSError, NotionImportError) as error:
        if promoted:
            _rollback(
                content_root,
                target,
                backup,
                candidate_fingerprint,
                previous_fingerprint,
            )
        elif backup_moved and backup is not None:
            assert previous_fingerprint is not None
            _verified_tree(backup, content_root, previous_fingerprint)
            try:
                os.replace(backup, target)
            except OSError as rollback_error:
                raise _global("recovery_failed", "unable to restore writing backup") from rollback_error
        if os.path.lexists(stage):
            _remove_verified_tree(stage, content_root, candidate_fingerprint)
        if isinstance(error, NotionImportError) and error.code == "recovery_failed":
            raise
        return (
            _result(
                candidate,
                "blocked",
                issue=ImportIssue(
                    candidate.source_ref,
                    "promotion_failed",
                    "Unable to promote writing bundle; prior content was restored",
                ),
            ),
            state,
        )

    next_sources = dict(state.sources)
    next_sources[key] = ImportStateEntry(
        key, candidate.slug, source_fingerprint, candidate_fingerprint
    )
    next_state = ImportState(1, next_sources)
    try:
        write_import_state(state_path, next_state)
    except NotionImportError:
        _rollback(
            content_root,
            target,
            backup,
            candidate_fingerprint,
            previous_fingerprint,
        )
        return (
            _result(
                candidate,
                "blocked",
                issue=ImportIssue(
                    candidate.source_ref,
                    "promotion_failed",
                    "Unable to persist private state; prior content was restored",
                ),
            ),
            state,
        )
    if backup is not None:
        assert previous_fingerprint is not None
        _remove_verified_tree(backup, content_root, previous_fingerprint)
    return (
        _result(
            candidate,
            "applied",
            source_fingerprint=source_fingerprint,
            written_fingerprint=candidate_fingerprint,
        ),
        next_state,
    )


def apply_import(
    inventory: ExportInventory,
    plan: ImportPlan,
    content_root: str | Path,
    state_path: str | Path,
    work_root: str | Path,
    report_path: str | Path,
) -> ImportRunResult:
    """Rebuild, guard, and atomically promote each independently valid article."""
    serialize_import_plan(plan)
    if inventory.fingerprint != plan.export_fingerprint:
        raise _global("invalid_plan", "export fingerprint does not match the import plan")
    content, state_file, work, report = _canonical_paths(
        content_root, state_path, work_root, report_path
    )
    _detect_residue(content)
    state = (
        load_import_state(state_file)
        if os.path.lexists(state_file)
        else ImportState(1, {})
    )
    apply_root, bundles, site = _reset_apply_workspace(work)
    try:
        prepared = prepare_import_candidates(inventory, plan, bundles, site)
        completed: list[ImportCandidateResult] = []
        _write_report(report, completed)
        for candidate in prepared.candidates:
            if candidate.status == "ready":
                try:
                    candidate, state = _promote_candidate(
                        candidate, inventory, content, state_file, state
                    )
                except NotionImportError as error:
                    if error.code == "recovery_failed":
                        raise
                    candidate = _result(
                        candidate,
                        "blocked",
                        issue=ImportIssue(
                            candidate.source_ref,
                            "promotion_failed",
                            "Unable to promote this writing bundle",
                        ),
                    )
            else:
                candidate = _result(candidate, candidate.status)
            completed.append(candidate)
            _write_report(report, completed)
        result = ImportRunResult(tuple(completed))
    except BaseException:
        try:
            _cleanup_apply_workspace(apply_root)
        except NotionImportError:
            pass
        raise
    _cleanup_apply_workspace(apply_root)
    return result
