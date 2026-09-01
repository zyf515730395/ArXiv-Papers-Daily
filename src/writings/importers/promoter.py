"""Guarded, dependency-aware promotion of rebuilt Notion writing bundles."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import BinaryIO, Callable

from . import durability
from .models import (
    CANDIDATE_STATUSES,
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
    unique_source_keys,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REPARSE_POINT = 0x0400
_STAGE_PREFIX = ".notion-import-stage-"
_BACKUP_PREFIX = ".notion-import-backup-"
_RESTORE_PREFIX = ".notion-import-restore-"
_TRASH_PREFIX = ".notion-import-trash-"
_MAX_GRAPH_NODES = 10_000
_LOCK_NAME = ".notion-import-apply.lock"
_JOURNAL_NAME = ".notion-import-transaction-v1.jsonl"
_WORKSPACE_OWNER = ".notion-import-owner-v1"
_WORKSPACE_TRASH_PREFIX = ".notion-import-workspace-trash-"
_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_PROCESS_IDENTITY = secrets.token_hex(16)
_APPLY_BOUNDARY_HOOK: Callable[[str], None] = lambda label: None


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
        supported = all(
            (
                durability.durable_mkdirs(content, "content-root"),
                durability.durable_mkdirs(work, "work-root"),
                durability.durable_mkdirs(report.parent, "report-root"),
            )
        )
    except OSError as error:
        raise _global("unsafe_root", "unable to create canonical import directories") from error
    if not supported:
        raise _global(
            "promotion_failed",
            "import directories cannot be made durable on this filesystem",
        )
    return content, state, work, report


def _detect_residue(
    content_root: Path,
    cleanup_evidence: tuple[_CleanupEvidence, ...] = (),
) -> None:
    allowed = {
        evidence.path for evidence in cleanup_evidence
    } | {
        _owned_trash_path(
            evidence.path, content_root, evidence.transaction_id
        )
        for evidence in cleanup_evidence
    }
    try:
        residue = [
            child
            for child in content_root.iterdir()
            if child.name.startswith(
                (_STAGE_PREFIX, _BACKUP_PREFIX, _RESTORE_PREFIX, _TRASH_PREFIX)
            )
        ]
    except OSError as error:
        raise _global("unsafe_root", "unable to inspect writing source root") from error
    if any(child not in allowed for child in residue):
        raise _global(
            "recovery_required",
            "import recovery residue requires human inspection before apply",
        )


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


def _file_bytes(path: Path) -> bytes | None:
    if not os.path.lexists(path):
        return None
    try:
        if _is_link_or_reparse(path) or not path.is_file():
            raise OSError("unsafe transaction file")
        return path.read_bytes()
    except OSError as error:
        raise _global(
            "recovery_failed", "transaction file cannot be observed"
        ) from error


def _scc_order(
    slugs: set[str], dependencies: dict[str, frozenset[str]]
) -> list[tuple[str, ...]]:
    """Return deterministic dependency-first SCCs without recursive traversal."""
    if len(slugs) > _MAX_GRAPH_NODES or any(
        not isinstance(slug, str) for slug in slugs
    ):
        raise _global("invalid_plan", "selected dependency graph exceeds safe limits")
    try:
        nodes = sorted(slugs)
        adjacency = {
            slug: tuple(sorted(dependencies.get(slug, frozenset()) & slugs))
            for slug in nodes
        }
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise _global("invalid_plan", "selected dependency graph is invalid") from error

    index = 0
    stack: list[str] = []
    indexes: dict[str, int] = {}
    low: dict[str, int] = {}
    active: set[str] = set()
    groups: list[tuple[str, ...]] = []

    for root in nodes:
        if root in indexes:
            continue
        indexes[root] = low[root] = index
        index += 1
        stack.append(root)
        active.add(root)
        frames: list[tuple[str, int, str | None]] = [(root, 0, None)]
        while frames:
            slug, position, parent = frames[-1]
            targets = adjacency[slug]
            if position < len(targets):
                target = targets[position]
                frames[-1] = (slug, position + 1, parent)
                if target not in indexes:
                    indexes[target] = low[target] = index
                    index += 1
                    stack.append(target)
                    active.add(target)
                    frames.append((target, 0, slug))
                elif target in active:
                    low[slug] = min(low[slug], indexes[target])
                continue
            frames.pop()
            if parent is not None:
                low[parent] = min(low[parent], low[slug])
            if low[slug] != indexes[slug]:
                continue
            group: list[str] = []
            while True:
                member = stack.pop()
                active.remove(member)
                group.append(member)
                if member == slug:
                    break
            groups.append(tuple(sorted(group)))

    group_for = {
        slug: number for number, group in enumerate(groups) for slug in group
    }
    group_dependencies = [set() for _ in groups]
    group_dependents = [set() for _ in groups]
    for slug in nodes:
        source_group = group_for[slug]
        for target in adjacency[slug]:
            target_group = group_for[target]
            if source_group == target_group:
                continue
            group_dependencies[source_group].add(target_group)
            group_dependents[target_group].add(source_group)
    ready = deque(
        sorted(
            (
                number
                for number, targets in enumerate(group_dependencies)
                if not targets
            ),
            key=lambda value: groups[value],
        )
    )
    ordered: list[tuple[str, ...]] = []
    while ready:
        number = ready.popleft()
        ordered.append(groups[number])
        for dependent in sorted(
            group_dependents[number], key=lambda value: groups[value]
        ):
            group_dependencies[dependent].discard(number)
            if not group_dependencies[dependent]:
                ready.append(dependent)
    if len(ordered) != len(groups):
        raise _global("invalid_plan", "selected dependency graph is invalid")
    return ordered


def _propagate_apply_unavailable(
    candidates: list[ImportCandidateResult],
    dependencies: dict[str, frozenset[str]],
) -> None:
    """Block dependents once through a reverse-edge queue."""
    index_by_slug = {
        candidate.slug: index
        for index, candidate in enumerate(candidates)
        if candidate.slug is not None
    }
    if len(index_by_slug) > _MAX_GRAPH_NODES:
        raise _global("invalid_plan", "selected dependency graph exceeds safe limits")
    dependents: dict[str, list[str]] = {slug: [] for slug in index_by_slug}
    try:
        for slug in index_by_slug:
            for target in dependencies.get(slug, frozenset()):
                if target in dependents:
                    dependents[target].append(slug)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise _global("invalid_plan", "selected dependency graph is invalid") from error
    status_by_slug = {
        slug: candidates[index].status for slug, index in index_by_slug.items()
    }
    unavailable = deque(
        sorted(
            slug
            for slug, status in status_by_slug.items()
            if status in {"blocked", "conflict"}
        )
    )
    while unavailable:
        target = unavailable.popleft()
        for slug in sorted(dependents[target]):
            if status_by_slug[slug] != "ready":
                continue
            index = index_by_slug[slug]
            candidates[index] = _blocked(
                candidates[index], "A selected page dependency is unavailable"
            )
            status_by_slug[slug] = "blocked"
            unavailable.append(slug)


def _lock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as error:
        raise _global("apply_locked", "another Notion apply is already running") from error


def _unlock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_lock_handle(path: Path, handle: BinaryIO) -> None:
    try:
        descriptor = os.fstat(handle.fileno())
        observed = path.stat(follow_symlinks=False)
        if (
            _is_link_or_reparse(path)
            or not stat.S_ISREG(descriptor.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or descriptor.st_nlink != 1
            or observed.st_nlink != 1
            or descriptor.st_size > 4096
            or (descriptor.st_dev, descriptor.st_ino)
            != (observed.st_dev, observed.st_ino)
        ):
            raise OSError("unsafe apply lock")
    except OSError as error:
        raise _global("recovery_required", "apply lock file is unsafe") from error


def _parse_lock_marker(data: bytes) -> str | None:
    if not data.strip():
        return None
    try:
        payload = json.loads(data.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise _global("recovery_required", "stale apply lock marker is invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "transaction_id"}
        or payload["version"] != 1
        or not isinstance(payload["transaction_id"], str)
        or not _TRANSACTION_ID.fullmatch(payload["transaction_id"])
    ):
        raise _global("recovery_required", "stale apply lock marker is invalid")
    return payload["transaction_id"]


@dataclass(slots=True)
class _ApplyLock:
    path: Path
    handle: BinaryIO
    stale_transaction_id: str | None
    marker_invalid: bool = False
    transaction_id: str | None = None
    clear_on_close: bool = False

    def activate(self, transaction_id: str) -> None:
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise _global("promotion_failed", "apply transaction identity is invalid")
        _validate_lock_handle(self.path, self.handle)
        self.transaction_id = transaction_id
        _APPLY_BOUNDARY_HOOK("apply:locked")

    def preserve(self) -> None:
        self.clear_on_close = False

    def mark_idle(self) -> None:
        """Mark this handle idle; durable recovery state lives only in the WAL."""
        _validate_lock_handle(self.path, self.handle)
        self.transaction_id = None
        self.clear_on_close = False

    def close(self) -> None:
        try:
            _unlock_handle(self.handle)
        finally:
            self.handle.close()


def _acquire_apply_lock(work_root: Path) -> _ApplyLock:
    path = work_root / _LOCK_NAME
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        _validate_lock_handle(path, handle)
        _lock_handle(handle)
        _validate_lock_handle(path, handle)
        handle.seek(0)
        marker = handle.read()
        try:
            stale_transaction_id = _parse_lock_marker(marker)
        except NotionImportError:
            stale_transaction_id = None
            marker_invalid = True
        else:
            marker_invalid = False
        return _ApplyLock(
            path,
            handle,
            stale_transaction_id,
            marker_invalid=marker_invalid,
        )
    except NotionImportError:
        if "handle" in locals() and not handle.closed:
            handle.close()
        raise
    except (OSError, RuntimeError) as error:
        if "handle" in locals() and not handle.closed:
            handle.close()
        raise _global("promotion_failed", "unable to acquire exclusive apply lock") from error


def _blob(data: bytes | None) -> dict[str, object]:
    return {
        "present": data is not None,
        "sha256": hashlib.sha256(data or b"").hexdigest(),
        "data": base64.b64encode(data or b"").decode("ascii"),
    }


def _unblob(value: object) -> bytes | None:
    if not isinstance(value, dict) or set(value) != {"present", "sha256", "data"}:
        raise _global("recovery_required", "transaction byte record is invalid")
    present = value["present"]
    digest = value["sha256"]
    encoded = value["data"]
    if (
        type(present) is not bool
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(encoded, str)
    ):
        raise _global("recovery_required", "transaction byte record is invalid")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise _global("recovery_required", "transaction byte record is invalid") from error
    if len(data) > 128 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise _global("recovery_required", "transaction byte record is invalid")
    if not present and data:
        raise _global("recovery_required", "transaction byte record is invalid")
    return data if present else None


def _journal_record(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    wrapper = {
        "checksum": hashlib.sha256(encoded).hexdigest(),
        "payload": payload,
    }
    return (
        json.dumps(
            wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )


def _read_journal(path: Path) -> list[dict[str, object]]:
    if not os.path.lexists(path):
        return []
    try:
        data = path.read_bytes()
    except OSError as error:
        raise _global("recovery_required", "transaction journal cannot be read") from error
    if not data:
        return []
    if not data.endswith(b"\n"):
        complete_end = data.rfind(b"\n") + 1
        try:
            with path.open("r+b") as handle:
                handle.truncate(complete_end)
                handle.flush()
                os.fsync(handle.fileno())
            durability._BOUNDARY_HOOK("journal:tail-truncate")
        except OSError as error:
            raise _global(
                "recovery_required", "transaction journal tail cannot be repaired"
            ) from error
        data = data[:complete_end]
        if not data:
            return []
    records: list[dict[str, object]] = []
    try:
        for raw in data.splitlines():
            wrapper = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(wrapper, dict)
                or set(wrapper) != {"checksum", "payload"}
                or not isinstance(wrapper["checksum"], str)
                or not isinstance(wrapper["payload"], dict)
            ):
                raise ValueError("invalid record")
            encoded = json.dumps(
                wrapper["payload"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if hashlib.sha256(encoded).hexdigest() != wrapper["checksum"]:
                raise ValueError("checksum mismatch")
            records.append(wrapper["payload"])
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise _global("recovery_required", "transaction journal is invalid") from error
    return records


@dataclass(slots=True)
class _Journal:
    path: Path
    transaction_id: str

    def start(
        self,
        state_before: bytes | None,
        report_before: bytes | None,
        workspace: str,
        workspace_token: str,
        cleanup_evidence: tuple[_CleanupEvidence, ...] = (),
    ) -> bool:
        data = _journal_record(
            {
                "kind": "header",
                "version": 1,
                "transaction_id": self.transaction_id,
                "process_id": _PROCESS_IDENTITY,
                "workspace": workspace,
                "workspace_token": workspace_token,
                "state_before": _blob(state_before),
                "report_before": _blob(report_before),
                "cleanup_evidence": [
                    _cleanup_evidence_payload(evidence)
                    for evidence in cleanup_evidence
                ],
            }
        )
        supported = durability.durable_atomic_write(
            self.path, data, "journal:header", self.transaction_id
        )
        durability._BOUNDARY_HOOK("journal:header")
        return supported

    def append(self, payload: dict[str, object], label: str) -> bool:
        return durability.durable_append(self.path, _journal_record(payload), label)

    def clear(self) -> bool:
        if not os.path.lexists(self.path):
            return durability.flush_directory(self.path.parent, "journal:clear:parent")
        return durability.durable_unlink(self.path, "journal:clear")


def _workspace_owner_matches(apply_root: Path, token: str) -> bool:
    owner = apply_root / _WORKSPACE_OWNER
    try:
        return (
            re.fullmatch(r"[0-9a-f]{64}", token) is not None
            and os.path.lexists(owner)
            and not _is_link_or_reparse(owner)
            and owner.is_file()
            and owner.read_bytes() == (token + "\n").encode("ascii")
        )
    except OSError:
        return False


def _partial_workspace_matches(
    apply_root: Path, token: str, transaction_id: str
) -> bool:
    """Recognize only the bounded tree reset can create before identity journaling."""
    owner = apply_root / _WORKSPACE_OWNER
    owner_temporary = re.compile(
        rf"^{re.escape('.' + _WORKSPACE_OWNER + '.' + transaction_id + '-')}")
    try:
        if _is_link_or_reparse(apply_root) or not apply_root.is_dir():
            return False
        for child in apply_root.iterdir():
            if child == owner:
                if not _workspace_owner_matches(apply_root, token):
                    return False
                continue
            if child.name in {"bundles", "site"}:
                if (
                    _is_link_or_reparse(child)
                    or not child.is_dir()
                    or any(child.iterdir())
                ):
                    return False
                continue
            if owner_temporary.match(child.name) and re.fullmatch(
                rf"{owner_temporary.pattern}[0-9a-f]{{16}}\.tmp", child.name
            ):
                if _is_link_or_reparse(child) or not child.is_file():
                    return False
                continue
            return False
        return True
    except (OSError, RuntimeError):
        return False


def _cleanup_apply_workspace(
    apply_root: Path,
    work_root: Path,
    token: str,
    identity: _TreeIdentity | None,
    transaction_id: str,
) -> bool:
    if (
        apply_root.parent != work_root
        or apply_root.name != f"apply-{transaction_id}"
        or not _TRANSACTION_ID.fullmatch(transaction_id)
    ):
        raise _global("recovery_failed", "private workspace path is invalid")
    trash = work_root / f"{_WORKSPACE_TRASH_PREFIX}{transaction_id}"
    freshly_renamed = False
    partial_workspace = False
    supported = True
    if os.path.lexists(apply_root):
        observed_identity = _tree_identity(apply_root)
        expected_identity = identity or observed_identity
        partial_workspace = identity is None and _partial_workspace_matches(
            apply_root, token, transaction_id
        )
        if (
            os.path.lexists(trash)
            or not _tree_has_identity(apply_root, work_root, expected_identity)
            or not (
                _workspace_owner_matches(apply_root, token) or partial_workspace
            )
        ):
            raise _global("recovery_failed", "private apply workspace is unsafe")
        try:
            supported = durability.durable_rename_noreplace(
                apply_root, trash, "workspace:trash"
            )
        except (OSError, RuntimeError) as error:
            raise _global(
                "recovery_failed", "private apply workspace cannot be quarantined"
            ) from error
        identity = expected_identity
        freshly_renamed = True
    elif not os.path.lexists(trash):
        return True
    if identity is None or not _tree_has_identity(trash, work_root, identity):
        if freshly_renamed and not os.path.lexists(apply_root):
            try:
                durability.durable_rename_noreplace(
                    trash, apply_root, "workspace:race-restore"
                )
            except (OSError, RuntimeError):
                pass
        raise _global("recovery_failed", "private workspace trash is unsafe")
    if freshly_renamed and not (
        _workspace_owner_matches(trash, token)
        or (
            partial_workspace
            and _partial_workspace_matches(trash, token, transaction_id)
        )
    ):
        raise _global("recovery_failed", "private workspace ownership changed")
    try:
        supported = durability.durable_remove_tree(
            trash, "workspace:trash-remove"
        ) and supported
    except OSError as error:
        raise _global(
            "recovery_failed", "private workspace cleanup is incomplete"
        ) from error
    if os.path.lexists(trash):
        raise _global("recovery_failed", "private workspace cleanup is incomplete")
    return supported


def _reset_transaction_workspace(
    work_root: Path, transaction_id: str, workspace_token: str
) -> tuple[Path, Path, Path, _TreeIdentity, bool]:
    apply_root = private_import_path(work_root / f"apply-{transaction_id}")
    if os.path.lexists(apply_root):
        raise _global("recovery_required", "transaction workspace already exists")
    bundles = apply_root / "bundles"
    site = apply_root / "site"
    try:
        supported = durability.durable_mkdirs(bundles, "workspace")
        supported = durability.durable_mkdirs(site, "workspace") and supported
        supported = durability.durable_atomic_write(
            apply_root / _WORKSPACE_OWNER,
            (workspace_token + "\n").encode("ascii"),
            "workspace:owner",
            transaction_id,
        ) and supported
    except OSError as error:
        raise _global("unsafe_root", "unable to create apply workspace") from error
    return apply_root, bundles, site, _tree_identity(apply_root), supported


@dataclass(frozen=True, slots=True)
class _TreeIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _CleanupEvidence:
    transaction_id: str
    slug: str
    path: Path
    fingerprint: str
    identity: _TreeIdentity


def _tree_identity(path: Path) -> _TreeIdentity:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as error:
        raise _global("recovery_failed", "writing tree identity cannot be observed") from error
    return _TreeIdentity(int(details.st_dev), int(details.st_ino))


def _identity_payload(identity: _TreeIdentity | None) -> list[int] | None:
    return None if identity is None else [identity.device, identity.inode]


def _payload_identity(value: object) -> _TreeIdentity | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise _global("recovery_required", "transaction tree identity is invalid")
    return _TreeIdentity(value[0], value[1])


def _tree_matches(
    path: Path,
    parent: Path,
    fingerprint: str,
    identity: _TreeIdentity,
) -> bool:
    if not os.path.lexists(path):
        return False
    try:
        return (
            Path(os.path.abspath(path)).parent == parent
            and not _is_link_or_reparse(path)
            and path.is_dir()
            and _tree_identity(path) == identity
            and fingerprint_bundle(path) == fingerprint
        )
    except (OSError, RuntimeError, NotionImportError):
        return False


def _tree_has_identity(
    path: Path,
    parent: Path,
    identity: _TreeIdentity,
) -> bool:
    if not os.path.lexists(path):
        return False
    try:
        return (
            Path(os.path.abspath(path)).parent == parent
            and not _is_link_or_reparse(path)
            and path.is_dir()
            and _tree_identity(path) == identity
        )
    except (OSError, RuntimeError, NotionImportError):
        return False


def _owned_trash_path(path: Path, parent: Path, transaction_id: str) -> Path:
    if (
        path.parent != parent
        or not _TRANSACTION_ID.fullmatch(transaction_id)
        or not path.name.startswith(
            tuple(
                f"{prefix}{transaction_id}-"
                for prefix in (_STAGE_PREFIX, _BACKUP_PREFIX, _RESTORE_PREFIX)
            )
        )
    ):
        raise _global("recovery_failed", "transaction tree path is invalid")
    return parent / f"{_TRASH_PREFIX}{path.name}"


def _remove_owned_tree(
    path: Path,
    parent: Path,
    fingerprint: str,
    identity: _TreeIdentity,
    label: str,
    transaction_id: str,
) -> bool:
    trash = _owned_trash_path(path, parent, transaction_id)
    freshly_renamed = False
    if os.path.lexists(path):
        if os.path.lexists(trash) or not _tree_matches(
            path, parent, fingerprint, identity
        ):
            raise _global(
                "recovery_failed", "transaction-owned writing tree cannot be proven"
            )
        try:
            supported = durability.durable_rename_noreplace(
                path, trash, f"{label}:trash"
            )
        except (OSError, RuntimeError) as error:
            raise _global(
                "recovery_failed", "transaction tree cannot be quarantined"
            ) from error
        freshly_renamed = True
    elif not os.path.lexists(trash):
        return True
    else:
        supported = True
    if not _tree_has_identity(trash, parent, identity) or (
        freshly_renamed and fingerprint_bundle(trash) != fingerprint
    ):
        if freshly_renamed and not os.path.lexists(path):
            try:
                durability.durable_rename_noreplace(
                    trash, path, f"{label}:race-restore"
                )
            except (OSError, RuntimeError):
                pass
        raise _global(
            "recovery_failed", "quarantined transaction tree cannot be proven"
        )
    try:
        supported = durability.durable_remove_tree(
            trash, f"{label}:trash-remove"
        ) and supported
    except OSError as error:
        raise _global(
            "recovery_failed", "quarantined transaction tree cleanup is incomplete"
        ) from error
    if os.path.lexists(trash):
        raise _global("recovery_failed", "transaction tree removal is incomplete")
    return supported


def _unique_transaction_sibling(
    content_root: Path, prefix: str, transaction_id: str, slug: str
) -> Path:
    try:
        for _ in range(32):
            candidate = content_root / (
                f"{prefix}{transaction_id}-{slug}-{secrets.token_hex(8)}"
            )
            if not os.path.lexists(candidate):
                return candidate
    except (OSError, RuntimeError) as error:
        raise _global("promotion_failed", "unable to allocate promotion path") from error
    raise _global("promotion_failed", "unable to allocate promotion path")


@dataclass(slots=True)
class _DurablePromotion:
    candidate: ImportCandidateResult
    key: str
    target: Path
    stage: Path
    backup: Path | None
    source_fingerprint: str
    new_fingerprint: str
    old_fingerprint: str | None
    stage_identity: _TreeIdentity
    old_identity: _TreeIdentity | None


def _promotion_payload(item: _DurablePromotion) -> dict[str, object]:
    return {
        "slug": item.candidate.slug,
        "stage": item.stage.name,
        "backup": item.backup.name if item.backup is not None else None,
        "source_fingerprint": item.source_fingerprint,
        "new_fingerprint": item.new_fingerprint,
        "old_fingerprint": item.old_fingerprint,
        "stage_identity": _identity_payload(item.stage_identity),
        "old_identity": _identity_payload(item.old_identity),
    }


def _safe_transaction_name(
    value: object,
    prefix: str,
    transaction_id: str,
    slug: str,
) -> str:
    if not isinstance(value, str) or not value.startswith(
        f"{prefix}{transaction_id}-{slug}-"
    ):
        raise _global("recovery_required", "transaction path identity is invalid")
    suffix = value.removeprefix(f"{prefix}{transaction_id}-{slug}-")
    if not re.fullmatch(r"[0-9a-f]{16}", suffix):
        raise _global("recovery_required", "transaction path identity is invalid")
    return value


@dataclass(frozen=True, slots=True)
class _RecoveryPromotion:
    slug: str
    target: Path
    stage: Path
    backup: Path | None
    new_fingerprint: str
    old_fingerprint: str | None
    stage_identity: _TreeIdentity
    old_identity: _TreeIdentity | None


def _cleanup_evidence_payload(
    evidence: _CleanupEvidence,
) -> dict[str, object]:
    return {
        "transaction_id": evidence.transaction_id,
        "slug": evidence.slug,
        "path": evidence.path.name,
        "fingerprint": evidence.fingerprint,
        "identity": _identity_payload(evidence.identity),
    }


def _cleanup_evidence_from_payload(
    value: object, content_root: Path
) -> _CleanupEvidence:
    if not isinstance(value, dict) or set(value) != {
        "transaction_id",
        "slug",
        "path",
        "fingerprint",
        "identity",
    }:
        raise _global("recovery_required", "cleanup evidence is invalid")
    transaction_id = value["transaction_id"]
    slug = value["slug"]
    fingerprint = value["fingerprint"]
    if (
        not isinstance(transaction_id, str)
        or not _TRANSACTION_ID.fullmatch(transaction_id)
        or not isinstance(slug, str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint)
    ):
        raise _global("recovery_required", "cleanup evidence is invalid")
    name = _safe_transaction_name(
        value["path"], _BACKUP_PREFIX, transaction_id, slug
    )
    identity = _payload_identity(value["identity"])
    if identity is None:
        raise _global("recovery_required", "cleanup evidence is invalid")
    return _CleanupEvidence(
        transaction_id,
        slug,
        content_root / name,
        fingerprint,
        identity,
    )


def _validated_cleanup_evidence(
    evidence: tuple[_CleanupEvidence, ...], content_root: Path
) -> tuple[_CleanupEvidence, ...]:
    retained: list[_CleanupEvidence] = []
    seen: set[Path] = set()
    for item in evidence:
        if item.path in seen:
            raise _global("recovery_required", "cleanup evidence is ambiguous")
        seen.add(item.path)
        trash = _owned_trash_path(
            item.path, content_root, item.transaction_id
        )
        path_exists = os.path.lexists(item.path)
        trash_exists = os.path.lexists(trash)
        if path_exists and trash_exists:
            raise _global("recovery_required", "cleanup evidence is duplicated")
        if path_exists and not _tree_matches(
            item.path, content_root, item.fingerprint, item.identity
        ):
            raise _global("recovery_required", "cleanup evidence is unsafe")
        if trash_exists and not _tree_has_identity(
            trash, content_root, item.identity
        ):
            raise _global("recovery_required", "cleanup evidence is unsafe")
        retained.append(item)
    return tuple(retained)


def _cleanup_retained_evidence(
    evidence: tuple[_CleanupEvidence, ...],
    content_root: Path,
    *,
    restart_observed: bool,
) -> tuple[tuple[_CleanupEvidence, ...], bool]:
    remaining: list[_CleanupEvidence] = []
    supported = True
    for item in _validated_cleanup_evidence(evidence, content_root):
        trash = _owned_trash_path(
            item.path, content_root, item.transaction_id
        )
        if not os.path.lexists(item.path) and not os.path.lexists(trash):
            if not restart_observed:
                remaining.append(item)
                supported = False
            continue
        removed = _remove_owned_tree(
            item.path,
            content_root,
            item.fingerprint,
            item.identity,
            "public:retained-backup-remove",
            item.transaction_id,
        )
        supported = removed and supported
        if not removed:
            remaining.append(item)
    return tuple(remaining), supported


def _backup_cleanup_evidence(
    active: list[tuple[int, list[_RecoveryPromotion]]],
    transaction_id: str,
) -> tuple[_CleanupEvidence, ...]:
    return tuple(
        _CleanupEvidence(
            transaction_id=transaction_id,
            slug=item.slug,
            path=item.backup,
            fingerprint=item.old_fingerprint,
            identity=item.old_identity,
        )
        for _, items in active
        for item in items
        if item.backup is not None
        and item.old_fingerprint is not None
        and item.old_identity is not None
    )


def _recovery_promotion(
    value: object, content_root: Path, transaction_id: str
) -> _RecoveryPromotion:
    if not isinstance(value, dict) or set(value) != {
        "slug",
        "stage",
        "backup",
        "source_fingerprint",
        "new_fingerprint",
        "old_fingerprint",
        "stage_identity",
        "old_identity",
    }:
        raise _global("recovery_required", "transaction promotion record is invalid")
    slug = value["slug"]
    if not isinstance(slug, str) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", slug
    ):
        raise _global("recovery_required", "transaction promotion record is invalid")
    new_fingerprint = value["new_fingerprint"]
    old_fingerprint = value["old_fingerprint"]
    fingerprint_pattern = r"sha256:[0-9a-f]{64}"
    if not isinstance(new_fingerprint, str) or not re.fullmatch(
        fingerprint_pattern, new_fingerprint
    ):
        raise _global("recovery_required", "transaction fingerprint is invalid")
    if old_fingerprint is not None and (
        not isinstance(old_fingerprint, str)
        or not re.fullmatch(fingerprint_pattern, old_fingerprint)
    ):
        raise _global("recovery_required", "transaction fingerprint is invalid")
    stage_name = _safe_transaction_name(
        value["stage"], _STAGE_PREFIX, transaction_id, slug
    )
    backup_value = value["backup"]
    backup = None
    if backup_value is not None:
        backup = content_root / _safe_transaction_name(
            backup_value, _BACKUP_PREFIX, transaction_id, slug
        )
    stage_identity = _payload_identity(value["stage_identity"])
    old_identity = _payload_identity(value["old_identity"])
    if stage_identity is None or ((old_fingerprint is None) != (old_identity is None)):
        raise _global("recovery_required", "transaction tree identity is invalid")
    return _RecoveryPromotion(
        slug,
        content_root / slug,
        content_root / stage_name,
        backup,
        new_fingerprint,
        old_fingerprint,
        stage_identity,
        old_identity,
    )


def _preflight_rollback(
    items: list[_RecoveryPromotion] | list[_DurablePromotion],
    content_root: Path,
    transaction_id: str,
) -> None:
    """Prove every SCC recovery source before the first destructive mutation."""
    for item in items:
        target_new = _tree_matches(
            item.target,
            content_root,
            item.new_fingerprint,
            item.stage_identity,
        )
        target_old = bool(
            item.old_fingerprint is not None
            and item.old_identity is not None
            and _tree_matches(
                item.target,
                content_root,
                item.old_fingerprint,
                item.old_identity,
            )
        )
        if os.path.lexists(item.target) and not (target_new or target_old):
            raise _global("recovery_failed", "writing target cannot be reconciled")
        stage_trash = _owned_trash_path(item.stage, content_root, transaction_id)
        if os.path.lexists(item.stage) and not _tree_matches(
            item.stage,
            content_root,
            item.new_fingerprint,
            item.stage_identity,
        ):
            raise _global("recovery_failed", "writing stage cannot be reconciled")
        if os.path.lexists(stage_trash) and not _tree_has_identity(
            stage_trash, content_root, item.stage_identity
        ):
            raise _global("recovery_failed", "writing stage trash is unsafe")
        if target_new and (
            os.path.lexists(item.stage) or os.path.lexists(stage_trash)
        ):
            raise _global("recovery_failed", "writing stage identity is duplicated")
        if item.old_fingerprint is not None:
            assert item.old_identity is not None and item.backup is not None
            backup_match = _tree_matches(
                item.backup,
                content_root,
                item.old_fingerprint,
                item.old_identity,
            )
            if os.path.lexists(item.backup) and not backup_match:
                raise _global("recovery_failed", "trusted writing backup is unsafe")
            if not target_old and not backup_match:
                raise _global("recovery_failed", "trusted writing backup is unavailable")
        elif item.backup is not None:
            raise _global("recovery_failed", "unexpected writing backup is recorded")


def _restore_promotion(
    item: _RecoveryPromotion | _DurablePromotion,
    content_root: Path,
    transaction_id: str,
) -> bool:
    target = item.target
    supported = True
    if _tree_matches(
        target, content_root, item.new_fingerprint, item.stage_identity
    ):
        try:
            supported = durability.durable_rename_noreplace(
                target, item.stage, "public:rollback-stage"
            ) and supported
        except (OSError, RuntimeError) as error:
            raise _global(
                "recovery_failed", "new writing target cannot be quarantined"
            ) from error
        if not _tree_matches(
            item.stage,
            content_root,
            item.new_fingerprint,
            item.stage_identity,
        ):
            if not os.path.lexists(target):
                try:
                    durability.durable_rename_noreplace(
                        item.stage, target, "public:rollback-race-restore"
                    )
                except (OSError, RuntimeError):
                    pass
            raise _global("recovery_failed", "new writing stage cannot be proven")
    if item.old_fingerprint is not None:
        assert item.old_identity is not None and item.backup is not None
        if not os.path.lexists(target):
            try:
                supported = durability.durable_rename_noreplace(
                    item.backup, target, "public:restore"
                ) and supported
            except (OSError, RuntimeError) as error:
                raise _global(
                    "recovery_failed", "trusted writing target cannot be restored"
                ) from error
        if not _tree_matches(
            target, content_root, item.old_fingerprint, item.old_identity
        ):
            raise _global("recovery_failed", "trusted writing target was not restored")
    elif os.path.lexists(target):
        raise _global("recovery_failed", "new writing target remains after rollback")
    supported = _remove_owned_tree(
        item.stage,
        content_root,
        item.new_fingerprint,
        item.stage_identity,
        "public:stage-remove",
        transaction_id,
    ) and supported
    return supported


def _restore_transaction_bytes(
    path: Path,
    data: bytes | None,
    label: str,
    transaction_id: str,
) -> bool:
    if data is None:
        return durability.durable_unlink(path, f"{label}:unlink")
    return durability.durable_atomic_write(path, data, label, transaction_id)


def _candidate_delta(
    indexes: list[int], candidates: list[ImportCandidateResult]
) -> list[dict[str, object]]:
    return [
        {
            "index": index,
            "status": candidates[index].status,
            "issues": [
                {"code": issue.code, "message": issue.message}
                for issue in candidates[index].issues
            ],
        }
        for index in indexes
    ]


def _materialize_recovery_report(
    baseline_report: bytes | None,
    result_records: list[dict[str, object]],
    rollback_slugs: set[str],
) -> bytes | None:
    if baseline_report is None:
        return None
    try:
        payload = json.loads(baseline_report.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("candidates"), list)
        ):
            raise ValueError("invalid report")
        candidates = payload["candidates"]
        for record in result_records:
            deltas = record.get("candidates")
            if not isinstance(deltas, list):
                raise ValueError("invalid result record")
            for delta in deltas:
                if (
                    not isinstance(delta, dict)
                    or set(delta) != {"index", "status", "issues"}
                    or type(delta["index"]) is not int
                    or not (0 <= delta["index"] < len(candidates))
                    or delta["status"] not in CANDIDATE_STATUSES
                    or not isinstance(delta["issues"], list)
                ):
                    raise ValueError("invalid result delta")
                candidate = candidates[delta["index"]]
                if not isinstance(candidate, dict):
                    raise ValueError("invalid report candidate")
                candidate["status"] = delta["status"]
                candidate["issues"] = delta["issues"]
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and (
                    candidate.get("status") == "ready"
                    or (
                        candidate.get("slug") in rollback_slugs
                        and candidate.get("status") == "applied"
                    )
                )
            ):
                candidate["status"] = "blocked"
                issues = candidate.setdefault("issues", [])
                if not isinstance(issues, list):
                    raise ValueError("invalid report issues")
                issues.append(
                    {
                        "code": "promotion_failed",
                        "message": "Interrupted import did not durably complete",
                    }
                )
        payload["counts"] = {
            status: sum(
                isinstance(candidate, dict)
                and candidate.get("status") == status
                for candidate in candidates
            )
            for status in CANDIDATE_STATUSES
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise _global("recovery_required", "transaction report cannot be rebuilt") from error


def _cleanup_transaction_temps(
    transaction_id: str, state_path: Path, report_path: Path
) -> bool:
    supported = True
    for parent, target in (
        (state_path.parent, state_path),
        (report_path.parent, report_path),
    ):
        prefix = f".{target.name}.{transaction_id}-"
        try:
            paths = [
                path
                for path in parent.iterdir()
                if path.name.startswith(prefix) and path.name.endswith(".tmp")
            ]
        except OSError as error:
            raise _global("recovery_required", "transaction temporary files are unsafe") from error
        for path in paths:
            try:
                if _is_link_or_reparse(path) or not path.is_file():
                    raise OSError("unsafe temporary")
                path.unlink()
                supported = durability.flush_directory(
                    parent, "recovery:temp-parent"
                ) and supported
            except OSError as error:
                raise _global(
                    "recovery_required", "transaction temporary file cannot be removed"
                ) from error
    return supported


def _parse_transaction(
    records: list[dict[str, object]],
    content_root: Path,
    expected_stale: str | None,
) -> tuple[
    dict[str, object],
    bytes | None,
    _TreeIdentity | None,
    list[tuple[int, list[_RecoveryPromotion]]],
    set[int],
    list[dict[str, object]],
    dict[str, object] | None,
    bool,
    tuple[_CleanupEvidence, ...],
    dict[str, object] | None,
]:
    if not records:
        raise _global("recovery_required", "stale apply lock has no transaction journal")
    header = records[0]
    legacy_header_fields = {
        "kind",
        "version",
        "transaction_id",
        "workspace",
        "workspace_token",
        "state_before",
        "report_before",
    }
    current_header_fields = legacy_header_fields | {
        "process_id",
        "cleanup_evidence",
    }
    if (
        frozenset(header)
        not in {frozenset(legacy_header_fields), frozenset(current_header_fields)}
        or (
            "process_id" in header
            and (
                not isinstance(header["process_id"], str)
                or not _TRANSACTION_ID.fullmatch(header["process_id"])
            )
        )
        or (
            "cleanup_evidence" in header
            and not isinstance(header["cleanup_evidence"], list)
        )
        or header.get("kind") != "header"
        or header.get("version") != 1
        or not isinstance(header.get("transaction_id"), str)
        or not _TRANSACTION_ID.fullmatch(header["transaction_id"])  # type: ignore[arg-type]
        or not isinstance(header.get("workspace"), str)
        or header["workspace"] != f"apply-{header['transaction_id']}"
        or not isinstance(header.get("workspace_token"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", header["workspace_token"])
    ):
        raise _global("recovery_required", "transaction journal header is invalid")
    cleanup_values = header.get("cleanup_evidence", [])
    assert isinstance(cleanup_values, list)
    if len(cleanup_values) > _MAX_GRAPH_NODES:
        raise _global("recovery_required", "cleanup evidence exceeds safe limits")
    cleanup_evidence = _validated_cleanup_evidence(
        tuple(
            _cleanup_evidence_from_payload(value, content_root)
            for value in cleanup_values
        ),
        content_root,
    )
    transaction_id = header["transaction_id"]
    if expected_stale is not None and transaction_id != expected_stale:
        raise _global("recovery_required", "stale lock and journal identities disagree")
    _unblob(header["state_before"])
    _unblob(header["report_before"])
    prepared: list[tuple[int, list[_RecoveryPromotion]]] = []
    baseline: bytes | None = None
    workspace_identity: _TreeIdentity | None = None
    rolled_back: set[int] = set()
    results: list[dict[str, object]] = []
    intent: dict[str, object] | None = None
    committed = False
    recovery_pending: dict[str, object] | None = None
    seen_groups: set[int] = set()
    for record in records[1:]:
        kind = record.get("kind")
        if recovery_pending is not None and kind != "recovery_pending":
            raise _global("recovery_required", "transaction recovery marker is invalid")
        if kind == "workspace":
            if (
                set(record) != {"kind", "identity"}
                or workspace_identity is not None
                or baseline is not None
                or prepared
                or results
                or intent is not None
                or committed
            ):
                raise _global("recovery_required", "transaction workspace is invalid")
            workspace_identity = _payload_identity(record["identity"])
            if workspace_identity is None:
                raise _global("recovery_required", "transaction workspace is invalid")
        elif kind == "baseline":
            if (
                set(record) != {"kind", "report"}
                or baseline is not None
                or prepared
                or results
                or intent is not None
                or committed
            ):
                raise _global("recovery_required", "transaction baseline is invalid")
            baseline = _unblob(record["report"])
            if baseline is None:
                raise _global("recovery_required", "transaction baseline is invalid")
        elif kind == "prepared":
            if set(record) != {"kind", "group", "items"}:
                raise _global("recovery_required", "transaction group record is invalid")
            group = record["group"]
            items = record["items"]
            if (
                type(group) is not int
                or group < 0
                or group in seen_groups
                or not isinstance(items, list)
                or not items
            ):
                raise _global("recovery_required", "transaction group record is invalid")
            seen_groups.add(group)
            prepared.append(
                (
                    group,
                    [
                        _recovery_promotion(item, content_root, transaction_id)
                        for item in items
                    ],
                )
            )
        elif kind == "rolled_back":
            if set(record) != {"kind", "group"} or record.get("group") not in seen_groups:
                raise _global("recovery_required", "transaction rollback record is invalid")
            rolled_back.add(record["group"])  # type: ignore[arg-type]
        elif kind == "result":
            if set(record) != {"kind", "group", "candidates"}:
                raise _global("recovery_required", "transaction result record is invalid")
            results.append(record)
        elif kind == "commit_intent":
            if set(record) != {"kind", "state_after", "report_after"} or intent is not None:
                raise _global("recovery_required", "transaction commit record is invalid")
            _unblob(record["state_after"])
            _unblob(record["report_after"])
            intent = record
        elif kind == "committed":
            if set(record) != {"kind"} or committed:
                raise _global("recovery_required", "transaction commit marker is invalid")
            committed = True
        elif kind == "recovery_pending":
            if (
                set(record) != {
                    "kind",
                    "process_id",
                    "outcome",
                    "state",
                    "report",
                }
                or not isinstance(record.get("process_id"), str)
                or not _TRANSACTION_ID.fullmatch(record["process_id"])  # type: ignore[arg-type]
                or record.get("outcome") not in {"committed", "rolled_back"}
            ):
                raise _global("recovery_required", "transaction recovery marker is invalid")
            _unblob(record["state"])
            _unblob(record["report"])
            recovery_pending = record
        else:
            raise _global("recovery_required", "transaction journal record is invalid")
    if committed and intent is None:
        raise _global("recovery_required", "committed journal has no commit intent")
    return (
        header,
        baseline,
        workspace_identity,
        prepared,
        rolled_back,
        results,
        intent,
        committed,
        cleanup_evidence,
        recovery_pending,
    )


@dataclass(frozen=True, slots=True)
class _RecoveryOutcome:
    transaction_id: str
    cleanup_evidence: tuple[_CleanupEvidence, ...]


def _rollback_is_observed(
    items: list[_RecoveryPromotion],
    content_root: Path,
    transaction_id: str,
) -> bool:
    try:
        for item in items:
            if item.old_fingerprint is None:
                if os.path.lexists(item.target):
                    return False
            else:
                assert item.old_identity is not None
                if not _tree_matches(
                    item.target,
                    content_root,
                    item.old_fingerprint,
                    item.old_identity,
                ):
                    return False
            residue = (
                item.stage,
                _owned_trash_path(item.stage, content_root, transaction_id),
            )
            if item.backup is not None:
                residue += (
                    item.backup,
                    _owned_trash_path(
                        item.backup, content_root, transaction_id
                    ),
                )
            if any(os.path.lexists(path) for path in residue):
                return False
        return True
    except (OSError, RuntimeError, NotionImportError):
        return False


def _pending_from_prior_process(
    pending: dict[str, object] | None,
    outcome: str,
    state: bytes | None,
    report: bytes | None,
) -> bool:
    if pending is None:
        return False
    if (
        pending.get("outcome") != outcome
        or _unblob(pending["state"]) != state
        or _unblob(pending["report"]) != report
    ):
        raise _global("recovery_required", "transaction recovery marker is invalid")
    process_id = pending["process_id"]
    assert isinstance(process_id, str)
    return process_id != _PROCESS_IDENTITY


def _defer_recovery(
    journal: _Journal,
    pending: dict[str, object] | None,
    outcome: str,
    state: bytes | None,
    report: bytes | None,
) -> None:
    if pending is None or pending.get("process_id") != _PROCESS_IDENTITY:
        journal.append(
            {
                "kind": "recovery_pending",
                "process_id": _PROCESS_IDENTITY,
                "outcome": outcome,
                "state": _blob(state),
                "report": _blob(report),
            },
            "journal:recovery-pending",
        )
    raise _global(
        "recovery_required",
        "recovered import must be verified by the next process before retry",
    )


def _recover_transaction(
    journal_path: Path,
    content_root: Path,
    state_path: Path,
    report_path: Path,
    work_root: Path,
    stale_transaction_id: str | None,
    marker_invalid: bool = False,
    *,
    expected_transaction_id: str | None = None,
) -> _RecoveryOutcome | None:
    if stale_transaction_id is not None or marker_invalid:
        raise _global(
            "recovery_required", "legacy apply lock marker requires human recovery"
        )
    records = _read_journal(journal_path)
    if not records:
        return None
    (
        header,
        baseline_report,
        workspace_identity,
        prepared,
        rolled_back,
        result_records,
        intent,
        committed,
        cleanup_evidence,
        recovery_pending,
    ) = _parse_transaction(
        records,
        content_root,
        expected_transaction_id,
    )
    transaction_id = header["transaction_id"]
    assert isinstance(transaction_id, str)
    active = [
        (group, items) for group, items in prepared if group not in rolled_back
    ]
    active_items = [item for _, items in active for item in items]
    all_items = [item for _, items in prepared for item in items]
    already_rolled_back = [
        item for group, items in prepared if group in rolled_back for item in items
    ]
    state_after = _unblob(intent["state_after"]) if intent is not None else None
    report_after = _unblob(intent["report_after"]) if intent is not None else None
    targets_forward = all(
        _tree_matches(
            item.target,
            content_root,
            item.new_fingerprint,
            item.stage_identity,
        )
        for item in active_items
    )
    journal = _Journal(journal_path, transaction_id)
    workspace = work_root / str(header["workspace"])
    workspace_token = header["workspace_token"]
    assert isinstance(workspace_token, str)
    header_process = header.get("process_id")
    restart_observed = not isinstance(header_process, str) or (
        header_process != _PROCESS_IDENTITY
    )
    cleanup_evidence = tuple(
        item
        for item in cleanup_evidence
        if not restart_observed
        or os.path.lexists(item.path)
        or os.path.lexists(
            _owned_trash_path(item.path, content_root, item.transaction_id)
        )
    )

    if committed:
        if intent is None:
            raise _global("recovery_required", "committed transaction has no intent")
        pending_prior = _pending_from_prior_process(
            recovery_pending,
            "committed",
            state_after,
            report_after,
        )
        recovery_supported = True

        if already_rolled_back and not _rollback_is_observed(
            already_rolled_back, content_root, transaction_id
        ):
            _preflight_rollback(
                already_rolled_back, content_root, transaction_id
            )
            for item in reversed(already_rolled_back):
                recovery_supported = _restore_promotion(
                    item, content_root, transaction_id
                ) and recovery_supported

        retained_targets = all(
            _tree_has_identity(item.target, content_root, item.stage_identity)
            for item in active_items
        )
        if not targets_forward and not (pending_prior and retained_targets):
            raise _global(
                "recovery_required", "committed transaction cannot be reconciled"
            )
        if any(os.path.lexists(item.stage) for item in active_items):
            raise _global("recovery_required", "committed stage residue remains")

        if _file_bytes(state_path) != state_after:
            recovery_supported = _restore_transaction_bytes(
                state_path, state_after, "state", transaction_id
            ) and recovery_supported
        if _file_bytes(report_path) != report_after:
            recovery_supported = _restore_transaction_bytes(
                report_path, report_after, "report", transaction_id
            ) and recovery_supported
        recovery_supported = _cleanup_apply_workspace(
            workspace,
            work_root,
            workspace_token,
            workspace_identity,
            transaction_id,
        ) and recovery_supported
        recovery_supported = _cleanup_transaction_temps(
            transaction_id, state_path, report_path
        ) and recovery_supported
        if not recovery_supported:
            _defer_recovery(
                journal,
                recovery_pending,
                "committed",
                state_after,
                report_after,
            )

        active_cleanup = _validated_cleanup_evidence(
            _backup_cleanup_evidence(active, transaction_id), content_root
        )
        if active_cleanup and not pending_prior:
            if not durability.flush_directory(
                content_root, "recovery:backup-proof"
            ):
                _defer_recovery(
                    journal,
                    recovery_pending,
                    "committed",
                    state_after,
                    report_after,
                )
            remaining, cleanup_supported = _cleanup_retained_evidence(
                active_cleanup,
                content_root,
                restart_observed=True,
            )
            if remaining or not cleanup_supported:
                _defer_recovery(
                    journal,
                    recovery_pending,
                    "committed",
                    state_after,
                    report_after,
                )
        elif active_cleanup:
            cleanup_evidence += active_cleanup
    else:
        state_before = _unblob(header["state_before"])
        report_before = _unblob(header["report_before"])
        rollback_slugs = {
            item.slug for _, items in prepared for item in items
        }
        recovered_report = _materialize_recovery_report(
            baseline_report if baseline_report is not None else report_before,
            result_records,
            rollback_slugs,
        )
        pending_prior = _pending_from_prior_process(
            recovery_pending,
            "rolled_back",
            state_before,
            recovered_report,
        )
        rollback_observed = (
            pending_prior
            and _rollback_is_observed(
                all_items, content_root, transaction_id
            )
            and _file_bytes(state_path) == state_before
            and _file_bytes(report_path) == recovered_report
        )
        recovery_supported = True
        if not rollback_observed:
            _preflight_rollback(all_items, content_root, transaction_id)
            for item in reversed(all_items):
                recovery_supported = _restore_promotion(
                    item, content_root, transaction_id
                ) and recovery_supported
            recovery_supported = _restore_transaction_bytes(
                state_path, state_before, "state", transaction_id
            ) and recovery_supported
            recovery_supported = _restore_transaction_bytes(
                report_path, recovered_report, "report", transaction_id
            ) and recovery_supported
        recovery_supported = _cleanup_apply_workspace(
            workspace,
            work_root,
            workspace_token,
            workspace_identity,
            transaction_id,
        ) and recovery_supported
        recovery_supported = _cleanup_transaction_temps(
            transaction_id, state_path, report_path
        ) and recovery_supported
        if not recovery_supported:
            _defer_recovery(
                journal,
                recovery_pending,
                "rolled_back",
                state_before,
                recovered_report,
            )

    return _RecoveryOutcome(transaction_id, cleanup_evidence)


def _copy_durable_stage(
    candidate: ImportCandidateResult,
    content_root: Path,
    transaction_id: str,
    source_fingerprint: str,
    new_fingerprint: str,
    old_fingerprint: str | None,
    old_identity: _TreeIdentity | None,
    backup: Path | None,
) -> _DurablePromotion:
    assert candidate.slug is not None and candidate.bundle_root is not None
    stage = _unique_transaction_sibling(
        content_root, _STAGE_PREFIX, transaction_id, candidate.slug
    )
    try:
        shutil.copytree(candidate.bundle_root, stage, symlinks=False)
        if fingerprint_bundle(stage) != new_fingerprint:
            raise OSError("staged bundle changed while copying")
        identity = _tree_identity(stage)
    except BaseException as error:
        if os.path.lexists(stage):
            try:
                observed = fingerprint_bundle(stage)
                identity = _tree_identity(stage)
                _remove_owned_tree(
                    stage,
                    content_root,
                    observed,
                    identity,
                    "public:stage-remove",
                    transaction_id,
                )
            except BaseException as cleanup_error:
                raise _global(
                    "recovery_failed", "unable to prove failed stage cleanup"
                ) from cleanup_error
        if not isinstance(error, Exception):
            raise
        raise _global("promotion_failed", "unable to stage writing bundle") from error
    return _DurablePromotion(
        candidate,
        source_key(candidate.source_ref),
        content_root / candidate.slug,
        stage,
        backup,
        source_fingerprint,
        new_fingerprint,
        old_fingerprint,
        identity,
        old_identity,
    )


_DurableMetadata = tuple[
    str,
    str,
    str | None,
    _TreeIdentity | None,
]


def _durable_preflight_candidate(
    candidate: ImportCandidateResult,
    inventory: ExportInventory,
    content_root: Path,
    state: ImportState,
    owners_by_slug: dict[str, ImportStateEntry],
) -> tuple[ImportCandidateResult, _DurableMetadata | None]:
    """Preflight once using run-wide state indexes and capture tree identity."""
    assert candidate.slug is not None and candidate.bundle_root is not None
    key = source_key(candidate.source_ref)
    entry = state.sources.get(key)
    owner = owners_by_slug.get(candidate.slug)
    target = content_root / candidate.slug
    source_fingerprint = "sha256:" + inventory.files[candidate.source_ref].sha256
    try:
        candidate_fingerprint = fingerprint_bundle(candidate.bundle_root)
    except NotionImportError:
        return _blocked(candidate, "Unable to verify rebuilt writing bundle"), None
    if entry is not None and entry.slug != candidate.slug:
        return _conflict(candidate, "Private state owns a different public slug"), None
    if owner is not None and owner != entry:
        return _conflict(candidate, "Public slug is owned by another import source"), None
    if os.path.lexists(target):
        if entry is None or owner != entry:
            return _conflict(candidate, "Existing writing bundle has no trusted state"), None
        try:
            old_identity = _tree_identity(target)
            previous = fingerprint_bundle(target)
            if _tree_identity(target) != old_identity:
                raise NotionImportError("invalid_state", "bundle changed")
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
        return (
            candidate,
            (source_fingerprint, candidate_fingerprint, previous, old_identity),
        )
    if entry is not None or owner is not None:
        return _conflict(candidate, "Private state and public bundle disagree"), None
    return candidate, (source_fingerprint, candidate_fingerprint, None, None)


def _discard_durable_stages(
    items: list[_DurablePromotion], content_root: Path, transaction_id: str
) -> bool:
    supported = True
    for item in reversed(items):
        if os.path.lexists(item.stage):
            supported = _remove_owned_tree(
                item.stage,
                content_root,
                item.new_fingerprint,
                item.stage_identity,
                "public:stage-remove",
                transaction_id,
            ) and supported
    return supported


def _promote_durable_group(
    group_number: int,
    candidates: list[ImportCandidateResult],
    metadata: dict[str, _DurableMetadata],
    content_root: Path,
    journal: _Journal,
    transaction_id: str,
) -> tuple[list[ImportCandidateResult], list[_DurablePromotion], bool]:
    """Durably promote one SCC, rolling back only identity-proven transaction trees."""
    items: list[_DurablePromotion] = []
    prepared = False
    supported = True
    try:
        backup_paths: dict[str, Path | None] = {}
        for candidate in candidates:
            assert candidate.slug is not None
            old_fingerprint = metadata[candidate.slug][2]
            backup_paths[candidate.slug] = (
                _unique_transaction_sibling(
                    content_root,
                    _BACKUP_PREFIX,
                    transaction_id,
                    candidate.slug,
                )
                if old_fingerprint is not None
                else None
            )
        for candidate in candidates:
            assert candidate.slug is not None
            source_fp, new_fp, old_fp, old_identity = metadata[candidate.slug]
            items.append(
                _copy_durable_stage(
                    candidate,
                    content_root,
                    transaction_id,
                    source_fp,
                    new_fp,
                    old_fp,
                    old_identity,
                    backup_paths[candidate.slug],
                )
            )
        for item in items:
            if item.old_fingerprint is None:
                if os.path.lexists(item.target):
                    raise _global(
                        "promotion_failed", "writing target changed after preflight"
                    )
            else:
                assert item.old_identity is not None
                if not _tree_matches(
                    item.target,
                    content_root,
                    item.old_fingerprint,
                    item.old_identity,
                ):
                    raise _global(
                        "promotion_failed", "writing target changed after preflight"
                    )
        supported = journal.append(
            {
                "kind": "prepared",
                "group": group_number,
                "items": [_promotion_payload(item) for item in items],
            },
            "journal:prepared",
        ) and supported
        prepared = True
        for item in items:
            supported = durability.make_tree_durable(item.stage, "stage") and supported
        for item in items:
            if item.old_fingerprint is not None:
                assert item.backup is not None and item.old_identity is not None
                supported = durability.durable_rename_noreplace(
                    item.target, item.backup, "public:backup"
                ) and supported
                if not _tree_matches(
                    item.backup,
                    content_root,
                    item.old_fingerprint,
                    item.old_identity,
                ):
                    raise _global(
                        "recovery_failed", "trusted writing backup cannot be proven"
                    )
            supported = durability.durable_rename_noreplace(
                item.stage, item.target, "public:promote"
            ) and supported
            if not _tree_matches(
                item.target,
                content_root,
                item.new_fingerprint,
                item.stage_identity,
            ):
                raise _global("recovery_failed", "promoted writing cannot be proven")
    except BaseException as error:
        try:
            if prepared:
                _preflight_rollback(items, content_root, transaction_id)
                for item in reversed(items):
                    supported = _restore_promotion(
                        item, content_root, transaction_id
                    ) and supported
                supported = journal.append(
                    {"kind": "rolled_back", "group": group_number},
                    "journal:rolled-back",
                ) and supported
            else:
                supported = _discard_durable_stages(
                    items, content_root, transaction_id
                ) and supported
        except BaseException as cleanup_error:
            raise _global(
                "recovery_failed", "dependency group rollback cannot be proven"
            ) from cleanup_error
        if not isinstance(error, Exception):
            raise
        if isinstance(error, NotionImportError) and error.code in {
            "recovery_failed",
            "recovery_required",
        }:
            raise
        return (
            [
                _blocked(
                    candidate,
                    "Unable to promote dependency group; prior content was restored",
                )
                for candidate in candidates
            ],
            [],
            supported,
        )
    return (
        [
            _result(
                item.candidate,
                "applied",
                source_fingerprint=item.source_fingerprint,
                written_fingerprint=item.new_fingerprint,
            )
            for item in items
        ],
        items,
        supported,
    )


def apply_import(
    inventory: ExportInventory,
    plan: ImportPlan,
    content_root: str | Path,
    state_path: str | Path,
    work_root: str | Path,
    report_path: str | Path,
) -> ImportRunResult:
    """Rebuild, preflight, and durably promote dependency groups."""
    serialize_import_plan(plan)
    if inventory.fingerprint != plan.export_fingerprint:
        raise _global("invalid_plan", "export fingerprint does not match the import plan")
    unique_source_keys(list(inventory.markdown_paths))
    content, state_file, work, report = _canonical_paths(
        content_root, state_path, work_root, report_path
    )
    journal_path = work / _JOURNAL_NAME
    lock = _acquire_apply_lock(work)
    journal: _Journal | None = None
    transaction_id: str | None = None
    transaction_active = False
    try:
        recovered = _recover_transaction(
            journal_path,
            content,
            state_file,
            report,
            work,
            lock.stale_transaction_id,
            lock.marker_invalid,
        )
        cleanup_evidence = (
            recovered.cleanup_evidence if recovered is not None else ()
        )
        if recovered is not None:
            lock.mark_idle()

        transaction_id = secrets.token_hex(16)
        workspace_token = secrets.token_hex(32)
        journal = _Journal(journal_path, transaction_id)
        state_before = _file_bytes(state_file)
        report_before = _file_bytes(report)
        durable_supported = journal.start(
            state_before,
            report_before,
            f"apply-{transaction_id}",
            workspace_token,
            cleanup_evidence,
        )
        lock.activate(transaction_id)
        transaction_active = True

        _detect_residue(content, cleanup_evidence)
        state = (
            load_import_state(state_file)
            if os.path.lexists(state_file)
            else ImportState(1, {})
        )
        apply_root, bundles, site, workspace_identity, workspace_supported = (
            _reset_transaction_workspace(work, transaction_id, workspace_token)
        )
        durable_supported = workspace_supported and durable_supported
        durable_supported = journal.append(
            {
                "kind": "workspace",
                "identity": _identity_payload(workspace_identity),
            },
            "journal:workspace",
        ) and durable_supported
        prepared = prepare_import_candidates(inventory, plan, bundles, site)
        results = list(prepared.candidates)
        owners_by_slug = {entry.slug: entry for entry in state.sources.values()}
        metadata: dict[str, _DurableMetadata] = {}
        for index, candidate in enumerate(results):
            if candidate.status != "ready":
                results[index] = _result(candidate, candidate.status)
                continue
            checked, details = _durable_preflight_candidate(
                candidate,
                inventory,
                content,
                state,
                owners_by_slug,
            )
            results[index] = checked
            if details is not None and checked.slug is not None:
                metadata[checked.slug] = details
        dependencies = dict(prepared.dependencies)
        _propagate_apply_unavailable(results, dependencies)
        baseline_report = serialize_import_report(
            ImportRunResult(tuple(results))
        ).encode("utf-8")
        durable_supported = journal.append(
            {"kind": "baseline", "report": _blob(baseline_report)},
            "journal:baseline",
        ) and durable_supported

        ready_slugs = {
            candidate.slug
            for candidate in results
            if candidate.status == "ready" and candidate.slug is not None
        }
        index_by_slug = {
            candidate.slug: index
            for index, candidate in enumerate(results)
            if candidate.slug is not None
        }
        status_by_slug = {
            slug: results[index].status for slug, index in index_by_slug.items()
        }
        next_sources = dict(state.sources)
        committed_items: list[_DurablePromotion] = []
        for group_number, group in enumerate(
            _scc_order(ready_slugs, dependencies)
        ):
            group_members = frozenset(group)
            group_indexes = [index_by_slug[slug] for slug in group]
            group_candidates = [results[index] for index in group_indexes]
            external = {
                target
                for slug in group
                for target in dependencies.get(slug, frozenset())
                if target not in group_members
            }
            if any(
                status_by_slug.get(target) not in {"applied", "unchanged"}
                for target in external
            ):
                completed = [
                    _blocked(candidate, "A selected page dependency failed to apply")
                    for candidate in group_candidates
                ]
                items: list[_DurablePromotion] = []
                group_supported = True
            else:
                completed, items, group_supported = _promote_durable_group(
                    group_number,
                    group_candidates,
                    metadata,
                    content,
                    journal,
                    transaction_id,
                )
            durable_supported = group_supported and durable_supported
            for index, candidate in zip(group_indexes, completed):
                results[index] = candidate
                if candidate.slug is not None:
                    status_by_slug[candidate.slug] = candidate.status
            for item in items:
                next_sources[item.key] = ImportStateEntry(
                    item.key,
                    item.candidate.slug or "",
                    item.source_fingerprint,
                    item.new_fingerprint,
                )
            committed_items.extend(items)
            durable_supported = journal.append(
                {
                    "kind": "result",
                    "group": group_number,
                    "candidates": _candidate_delta(group_indexes, results),
                },
                "journal:result",
            ) and durable_supported

        next_state = ImportState(1, next_sources)
        state_after = (
            serialize_import_state(next_state).encode("utf-8")
            if next_state != state
            else state_before
        )
        final_result = ImportRunResult(tuple(results), prepared.dependencies)
        report_after = serialize_import_report(final_result).encode("utf-8")
        durable_supported = journal.append(
            {
                "kind": "commit_intent",
                "state_after": _blob(state_after),
                "report_after": _blob(report_after),
            },
            "journal:commit-intent",
        ) and durable_supported
        if state_after != state_before:
            assert state_after is not None
            durable_supported = durability.durable_atomic_write(
                state_file, state_after, "state", transaction_id
            ) and durable_supported
        durable_supported = durability.durable_atomic_write(
            report, report_after, "report", transaction_id
        ) and durable_supported
        if (
            _file_bytes(state_file) != state_after
            or _file_bytes(report) != report_after
            or any(
                not _tree_matches(
                    item.target,
                    content,
                    item.new_fingerprint,
                    item.stage_identity,
                )
                for item in committed_items
            )
        ):
            raise _global("recovery_failed", "committed import cannot be proven")
        durable_supported = journal.append(
            {"kind": "committed"}, "journal:committed"
        ) and durable_supported

        if durable_supported and cleanup_evidence:
            _, retained_supported = _cleanup_retained_evidence(
                cleanup_evidence,
                content,
                restart_observed=False,
            )
            durable_supported = retained_supported and durable_supported
        if durable_supported:
            for item in committed_items:
                if item.backup is not None:
                    assert item.old_fingerprint is not None and item.old_identity is not None
                    durable_supported = _remove_owned_tree(
                        item.backup,
                        content,
                        item.old_fingerprint,
                        item.old_identity,
                        "public:backup-remove",
                        transaction_id,
                    ) and durable_supported
        durable_supported = _cleanup_apply_workspace(
            apply_root,
            work,
            workspace_token,
            workspace_identity,
            transaction_id,
        ) and durable_supported
        lock.mark_idle()
        transaction_active = False
        if durable_supported:
            journal.clear()
        return final_result
    except BaseException as error:
        if transaction_active and transaction_id is not None and journal is not None:
            try:
                _recover_transaction(
                    journal_path,
                    content,
                    state_file,
                    report,
                    work,
                    None,
                    expected_transaction_id=transaction_id,
                )
                lock.mark_idle()
                transaction_active = False
            except BaseException as recovery_error:
                lock.preserve()
                if not isinstance(error, Exception):
                    if hasattr(error, "add_note"):
                        error.add_note("Durable import recovery also failed")
                    raise error
                raise _global(
                    "recovery_required",
                    "interrupted import requires recovery before retry",
                ) from recovery_error
        raise
    finally:
        lock.close()
