"""Guarded, dependency-aware promotion of rebuilt Notion writing bundles."""

from __future__ import annotations

from dataclasses import dataclass
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
    serialize_import_state,
    source_key,
    write_import_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REPARSE_POINT = 0x0400
_STAGE_PREFIX = ".notion-import-stage-"
_BACKUP_PREFIX = ".notion-import-backup-"
_RESTORE_PREFIX = ".notion-import-restore-"


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
        report_path,
        project / "build" / "reports" / "notion-import.json",
        "unsafe_report",
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
            if child.name.startswith(
                (_STAGE_PREFIX, _BACKUP_PREFIX, _RESTORE_PREFIX)
            )
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
    except (OSError, RuntimeError) as error:
        raise _global("promotion_failed", "unable to refresh private import report") from error


def _result(
    candidate: ImportCandidateResult,
    status: CandidateStatus,
    *,
    issue: ImportIssue | None = None,
    source_fingerprint: str | None = None,
    written_fingerprint: str | None = None,
) -> ImportCandidateResult:
    return ImportCandidateResult(
        candidate.source_ref,
        candidate.slug,
        status,
        candidate.issues + ((issue,) if issue else ()),
        None,
        source_fingerprint,
        written_fingerprint,
    )


def _blocked(candidate: ImportCandidateResult, message: str) -> ImportCandidateResult:
    return _result(
        candidate,
        "blocked",
        issue=ImportIssue(candidate.source_ref, "promotion_failed", message),
    )


def _conflict(candidate: ImportCandidateResult, message: str) -> ImportCandidateResult:
    return _result(
        candidate,
        "conflict",
        issue=ImportIssue(candidate.source_ref, "bundle_conflict", message),
    )


def _unique_sibling(content_root: Path, prefix: str, slug: str) -> Path:
    try:
        for _ in range(32):
            candidate = content_root / f"{prefix}{slug}-{secrets.token_hex(8)}"
            if not os.path.lexists(candidate):
                return candidate
    except (OSError, RuntimeError) as error:
        raise _global("promotion_failed", "unable to allocate a unique promotion path") from error
    raise _global("promotion_failed", "unable to allocate a unique promotion path")


def _tree_state(path: Path, parent: Path, expected: str) -> str:
    if not os.path.lexists(path):
        return "absent"
    try:
        lexical = Path(os.path.abspath(path))
        if lexical.parent != parent or _is_link_or_reparse(path) or not path.is_dir():
            return "other"
        return "match" if fingerprint_bundle(path) == expected else "other"
    except (OSError, RuntimeError, NotionImportError):
        return "other"


def _remove_matching_tree(path: Path, parent: Path, expected: str) -> None:
    state = _tree_state(path, parent, expected)
    if state == "absent":
        return
    if state != "match":
        raise _global("recovery_failed", "promotion residue cannot be verified")
    try:
        shutil.rmtree(path)
    except OSError:
        if not os.path.lexists(path):
            return
        raise _global("recovery_failed", "unable to remove verified promotion residue")
    if os.path.lexists(path):
        raise _global("recovery_failed", "promotion residue removal cannot be proven")


def _copy_stage(source: Path, content_root: Path, slug: str, expected: str) -> Path:
    stage = _unique_sibling(content_root, _STAGE_PREFIX, slug)
    try:
        shutil.copytree(source, stage, symlinks=False)
        if _tree_state(stage, content_root, expected) != "match":
            raise OSError("staged bundle changed while copying")
    except BaseException as error:
        try:
            _remove_matching_tree(stage, content_root, expected)
        except NotionImportError as cleanup_error:
            raise _global(
                "recovery_failed", "unable to prove failed stage cleanup"
            ) from cleanup_error
        if not isinstance(error, Exception):
            raise
        if isinstance(error, NotionImportError) and error.code == "recovery_failed":
            raise
        raise _global("promotion_failed", "unable to stage writing bundle") from error
    return stage


@dataclass(slots=True)
class _Promotion:
    candidate: ImportCandidateResult
    key: str
    target: Path
    stage: Path
    backup: Path | None
    source_fingerprint: str
    new_fingerprint: str
    old_fingerprint: str | None


def _restore_old_target(item: _Promotion, content_root: Path) -> None:
    target_new = _tree_state(item.target, content_root, item.new_fingerprint)
    target_old = (
        "absent"
        if item.old_fingerprint is None
        else _tree_state(item.target, content_root, item.old_fingerprint)
    )
    if item.old_fingerprint is None:
        if target_new == "match":
            _remove_matching_tree(item.target, content_root, item.new_fingerprint)
        elif target_new != "absent":
            raise _global("recovery_failed", "new writing target cannot be reconciled")
        if item.backup is not None and os.path.lexists(item.backup):
            raise _global("recovery_failed", "unexpected promotion backup remains")
    else:
        assert item.backup is not None
        backup_state = _tree_state(item.backup, content_root, item.old_fingerprint)
        if target_old == "match":
            if backup_state == "match":
                _remove_matching_tree(item.backup, content_root, item.old_fingerprint)
            elif backup_state != "absent":
                raise _global("recovery_failed", "writing backup cannot be reconciled")
        else:
            if target_new == "match":
                _remove_matching_tree(item.target, content_root, item.new_fingerprint)
            elif target_new != "absent":
                raise _global("recovery_failed", "changed writing target cannot be reconciled")
            if backup_state != "match":
                raise _global("recovery_failed", "trusted writing backup is unavailable")
            restore = _unique_sibling(
                content_root, _RESTORE_PREFIX, item.candidate.slug or "writing"
            )
            try:
                shutil.copytree(item.backup, restore, symlinks=False)
                if _tree_state(restore, content_root, item.old_fingerprint) != "match":
                    raise OSError("restore copy changed")
                os.replace(restore, item.target)
            except BaseException as error:
                if _tree_state(item.target, content_root, item.old_fingerprint) != "match":
                    raise _global(
                        "recovery_failed", "unable to restore trusted writing target"
                    ) from error
            if _tree_state(item.target, content_root, item.old_fingerprint) != "match":
                raise _global(
                    "recovery_failed",
                    "restored writing fingerprint does not match state",
                )
            if os.path.lexists(restore):
                _remove_matching_tree(restore, content_root, item.old_fingerprint)
            _remove_matching_tree(item.backup, content_root, item.old_fingerprint)
    if _tree_state(item.stage, content_root, item.new_fingerprint) == "match":
        _remove_matching_tree(item.stage, content_root, item.new_fingerprint)
    elif os.path.lexists(item.stage):
        raise _global("recovery_failed", "promotion stage cannot be reconciled")


def _rollback_group(items: list[_Promotion], content_root: Path) -> None:
    failure: NotionImportError | None = None
    for item in reversed(items):
        try:
            _restore_old_target(item, content_root)
        except NotionImportError as error:
            failure = error
    if failure is not None:
        raise _global("recovery_failed", "group rollback cannot be proven") from failure


def _file_bytes(path: Path) -> bytes | None:
    if not os.path.lexists(path):
        return None
    try:
        if _is_link_or_reparse(path) or not path.is_file():
            raise OSError("unsafe transaction file")
        return path.read_bytes()
    except OSError as error:
        raise _global("recovery_failed", "transaction file cannot be observed") from error


def _atomic_text_bytes(value: str) -> bytes:
    """Mirror Path.write_text newline translation used by atomic_write_text."""
    return value.replace("\n", os.linesep).encode("utf-8")


def _restore_state_file(
    path: Path,
    old_state: ImportState,
    old_bytes: bytes | None,
    new_bytes: bytes,
) -> None:
    current = _file_bytes(path)
    if current == old_bytes:
        return
    if current != new_bytes:
        raise _global("recovery_failed", "private state cannot be reconciled")
    if old_bytes is None:
        try:
            path.unlink()
        except OSError as error:
            if os.path.lexists(path):
                raise _global("recovery_failed", "private state rollback failed") from error
    else:
        try:
            write_import_state(path, old_state)
        except BaseException as error:
            if _file_bytes(path) != old_bytes:
                raise _global(
                    "recovery_failed", "private state rollback cannot be proven"
                ) from error
    if _file_bytes(path) != old_bytes:
        raise _global("recovery_failed", "private state rollback cannot be proven")


def _preflight_candidate(
    candidate: ImportCandidateResult,
    inventory: ExportInventory,
    content_root: Path,
    state: ImportState,
) -> tuple[ImportCandidateResult, tuple[str, str, str | None] | None]:
    assert candidate.slug is not None and candidate.bundle_root is not None
    key = source_key(candidate.source_ref)
    entry = state.sources.get(key)
    owners = [item for item in state.sources.values() if item.slug == candidate.slug]
    target = content_root / candidate.slug
    source_fingerprint = "sha256:" + inventory.files[candidate.source_ref].sha256
    try:
        candidate_fingerprint = fingerprint_bundle(candidate.bundle_root)
    except NotionImportError:
        return _blocked(candidate, "Unable to verify rebuilt writing bundle"), None
    if entry is not None and entry.slug != candidate.slug:
        return _conflict(candidate, "Private state owns a different public slug"), None
    if owners and (entry is None or len(owners) != 1 or owners[0] != entry):
        return _conflict(candidate, "Public slug is owned by another import source"), None
    if os.path.lexists(target):
        if entry is None or len(owners) != 1:
            return _conflict(candidate, "Existing writing bundle has no trusted state"), None
        try:
            previous = fingerprint_bundle(target)
        except NotionImportError:
            return _conflict(candidate, "Existing writing bundle is unsafe"), None
        if previous != entry.written_fingerprint:
            return _conflict(candidate, "Existing writing bundle contains human edits"), None
        if candidate_fingerprint == previous:
            return (
                _result(
                    candidate,
                    "unchanged",
                    source_fingerprint=source_fingerprint,
                    written_fingerprint=candidate_fingerprint,
                ),
                None,
            )
        return candidate, (source_fingerprint, candidate_fingerprint, previous)
    if entry is not None or owners:
        return _conflict(candidate, "Private state and public bundle disagree"), None
    return candidate, (source_fingerprint, candidate_fingerprint, None)


def _scc_order(slugs: set[str], dependencies: dict[str, frozenset[str]]) -> list[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    indexes: dict[str, int] = {}
    low: dict[str, int] = {}
    active: set[str] = set()
    groups: list[tuple[str, ...]] = []

    def visit(slug: str) -> None:
        nonlocal index
        indexes[slug] = low[slug] = index
        index += 1
        stack.append(slug)
        active.add(slug)
        for target in sorted(dependencies.get(slug, frozenset()) & slugs):
            if target not in indexes:
                visit(target)
                low[slug] = min(low[slug], low[target])
            elif target in active:
                low[slug] = min(low[slug], indexes[target])
        if low[slug] == indexes[slug]:
            group: list[str] = []
            while True:
                member = stack.pop()
                active.remove(member)
                group.append(member)
                if member == slug:
                    break
            groups.append(tuple(sorted(group)))

    for slug in sorted(slugs):
        if slug not in indexes:
            visit(slug)
    group_for = {slug: number for number, group in enumerate(groups) for slug in group}
    ordered: list[tuple[str, ...]] = []
    visited: set[int] = set()

    def append_dependencies(number: int) -> None:
        if number in visited:
            return
        visited.add(number)
        external = {
            group_for[target]
            for slug in groups[number]
            for target in dependencies.get(slug, frozenset()) & slugs
            if group_for[target] != number
        }
        for target_number in sorted(external, key=lambda value: groups[value]):
            append_dependencies(target_number)
        ordered.append(groups[number])

    for number in sorted(range(len(groups)), key=lambda value: groups[value]):
        append_dependencies(number)
    return ordered


def _promote_group(
    candidates: list[ImportCandidateResult],
    metadata: dict[str, tuple[str, str, str | None]],
    content_root: Path,
    state_path: Path,
    report_path: Path,
    state: ImportState,
    all_results: list[ImportCandidateResult],
) -> tuple[list[ImportCandidateResult], ImportState, bool]:
    items: list[_Promotion] = []
    try:
        for candidate in candidates:
            assert candidate.slug is not None and candidate.bundle_root is not None
            source_fp, new_fp, old_fp = metadata[candidate.slug]
            stage = _copy_stage(candidate.bundle_root, content_root, candidate.slug, new_fp)
            backup = (
                _unique_sibling(content_root, _BACKUP_PREFIX, candidate.slug)
                if old_fp is not None
                else None
            )
            items.append(
                _Promotion(
                    candidate,
                    source_key(candidate.source_ref),
                    content_root / candidate.slug,
                    stage,
                    backup,
                    source_fp,
                    new_fp,
                    old_fp,
                )
            )
    except BaseException as error:
        _rollback_group(items, content_root)
        if not isinstance(error, Exception):
            raise
        return (
            [
                _blocked(candidate, "Unable to stage dependency group")
                for candidate in candidates
            ],
            state,
            False,
        )
    try:
        for item in items:
            if item.old_fingerprint is not None:
                assert item.backup is not None
                os.replace(item.target, item.backup)
                if _tree_state(item.backup, content_root, item.old_fingerprint) != "match":
                    raise OSError("backup verification failed")
            os.replace(item.stage, item.target)
            if _tree_state(item.target, content_root, item.new_fingerprint) != "match":
                raise OSError("promotion verification failed")
    except BaseException as error:
        _rollback_group(items, content_root)
        if not isinstance(error, Exception):
            raise
        return (
            [
                _blocked(
                    candidate,
                    "Unable to promote dependency group; prior content was restored",
                )
                for candidate in candidates
            ],
            state,
            False,
        )
    next_sources = dict(state.sources)
    for item in items:
        next_sources[item.key] = ImportStateEntry(
            item.key,
            item.candidate.slug or "",
            item.source_fingerprint,
            item.new_fingerprint,
        )
    next_state = ImportState(1, next_sources)
    old_state_bytes = _file_bytes(state_path)
    new_state_bytes = serialize_import_state(next_state).encode("utf-8")
    try:
        write_import_state(state_path, next_state)
        if _file_bytes(state_path) != new_state_bytes:
            raise OSError("state commit verification failed")
    except BaseException as error:
        _restore_state_file(state_path, state, old_state_bytes, new_state_bytes)
        _rollback_group(items, content_root)
        if not isinstance(error, Exception):
            raise
        return (
            [
                _blocked(
                    candidate,
                    "Unable to persist private state; prior content was restored",
                )
                for candidate in candidates
            ],
            state,
            False,
        )
    applied = [
        _result(
            item.candidate,
            "applied",
            source_fingerprint=item.source_fingerprint,
            written_fingerprint=item.new_fingerprint,
        )
        for item in items
    ]
    by_ref = {candidate.source_ref: candidate for candidate in applied}
    prospective = [by_ref.get(candidate.source_ref, candidate) for candidate in all_results]
    old_report_bytes = _file_bytes(report_path)
    intended_report = _atomic_text_bytes(
        serialize_import_report(ImportRunResult(tuple(prospective)))
    )
    try:
        _write_report(report_path, prospective)
        if _file_bytes(report_path) != intended_report:
            raise OSError("report commit verification failed")
    except BaseException as error:
        _restore_state_file(state_path, state, old_state_bytes, new_state_bytes)
        _rollback_group(items, content_root)
        blocked = [
            _blocked(
                candidate,
                "Unable to record dependency group; prior content was restored",
            )
            for candidate in candidates
        ]
        blocked_by_ref = {candidate.source_ref: candidate for candidate in blocked}
        blocked_report = [
            blocked_by_ref.get(candidate.source_ref, candidate)
            for candidate in all_results
        ]
        intended_blocked = _atomic_text_bytes(
            serialize_import_report(ImportRunResult(tuple(blocked_report)))
        )
        try:
            _write_report(report_path, blocked_report)
        except BaseException as report_error:
            observed = _file_bytes(report_path)
            if observed != intended_blocked:
                if observed == old_report_bytes:
                    if not isinstance(error, Exception):
                        raise error
                    if not isinstance(report_error, Exception):
                        raise report_error
                    raise _global(
                        "promotion_failed",
                        "unable to record rolled-back import group",
                    ) from report_error
                raise _global(
                    "recovery_required",
                    "private report state cannot be reconciled",
                ) from report_error
        if _file_bytes(report_path) != intended_blocked:
            raise _global(
                "recovery_required", "private report state cannot be reconciled"
            ) from error
        if not isinstance(error, Exception):
            raise
        return blocked, state, True
    for item in items:
        if item.backup is not None:
            try:
                _remove_matching_tree(item.backup, content_root, item.old_fingerprint or "")
            except NotionImportError as error:
                raise _global(
                    "recovery_required", "committed import backup requires recovery"
                ) from error
        if os.path.lexists(item.stage):
            raise _global("recovery_required", "committed import stage requires recovery")
    return applied, next_state, True


def _cleanup_apply_workspace(apply_root: Path) -> None:
    if not os.path.lexists(apply_root):
        return
    if _is_link_or_reparse(apply_root) or not apply_root.is_dir():
        raise _global("promotion_failed", "private apply workspace is unsafe")
    try:
        shutil.rmtree(apply_root)
    except OSError as error:
        raise _global("promotion_failed", "unable to clean private apply workspace") from error
    if os.path.lexists(apply_root):
        raise _global("promotion_failed", "private apply workspace cleanup is incomplete")


def apply_import(
    inventory: ExportInventory,
    plan: ImportPlan,
    content_root: str | Path,
    state_path: str | Path,
    work_root: str | Path,
    report_path: str | Path,
) -> ImportRunResult:
    """Rebuild, preflight, and transactionally promote dependency groups."""
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
        results = list(prepared.candidates)
        metadata: dict[str, tuple[str, str, str | None]] = {}
        for index, candidate in enumerate(results):
            if candidate.status != "ready":
                results[index] = _result(candidate, candidate.status)
                continue
            checked, details = _preflight_candidate(candidate, inventory, content, state)
            results[index] = checked
            if details is not None and checked.slug is not None:
                metadata[checked.slug] = details
        status_by_slug = {
            candidate.slug: candidate.status
            for candidate in results
            if candidate.slug is not None
        }
        changed = True
        while changed:
            changed = False
            for index, candidate in enumerate(results):
                if candidate.status != "ready" or candidate.slug is None:
                    continue
                if any(
                    status_by_slug.get(target) in {"blocked", "conflict"}
                    for target in prepared.dependencies.get(
                        candidate.slug, frozenset()
                    )
                ):
                    results[index] = _blocked(
                        candidate, "A selected page dependency is unavailable"
                    )
                    status_by_slug[candidate.slug] = "blocked"
                    changed = True
        _write_report(report, results)
        ready_slugs = {
            candidate.slug
            for candidate in results
            if candidate.status == "ready" and candidate.slug is not None
        }
        for group in _scc_order(ready_slugs, dict(prepared.dependencies)):
            group_indexes = [
                index
                for index, candidate in enumerate(results)
                if candidate.slug in group
            ]
            group_candidates = [results[index] for index in group_indexes]
            external = {
                target
                for slug in group
                for target in prepared.dependencies.get(slug, frozenset())
                if target not in group
            }
            current_status = {
                candidate.slug: candidate.status
                for candidate in results
                if candidate.slug is not None
            }
            if any(
                current_status.get(target) not in {"applied", "unchanged"}
                for target in external
            ):
                completed = [
                    _blocked(candidate, "A selected page dependency failed to apply")
                    for candidate in group_candidates
                ]
                report_committed = False
            else:
                completed, state, report_committed = _promote_group(
                    group_candidates,
                    metadata,
                    content,
                    state_file,
                    report,
                    state,
                    results,
                )
            for index, candidate in zip(group_indexes, completed):
                results[index] = candidate
            if not report_committed:
                _write_report(report, results)
        result = ImportRunResult(tuple(results), prepared.dependencies)
    except BaseException as error:
        try:
            _cleanup_apply_workspace(apply_root)
        except NotionImportError:
            if hasattr(error, "add_note"):
                error.add_note("Private apply workspace cleanup also failed")
        raise
    _cleanup_apply_workspace(apply_root)
    return result
