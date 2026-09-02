"""Redacted aggregation of local writing workflow evidence."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any

from writings.catalog import WritingCatalogError, validate_writing_bundle
from writings.importers.models import PROJECT_ROOT, WritingImportError
from writings.importers.state import fingerprint_bundle

from .models import (
    BuildSummary,
    DraftRecord,
    SourceSummary,
    WorkbenchError,
    WorkbenchStatus,
)
from .paths import draft_root
from .state import load_reviews


_CANDIDATE_STATUSES = {"ready", "unchanged", "conflict", "blocked", "ignored", "applied"}


def _adapter_summary(source: str, report_name: str) -> SourceSummary:
    path = Path(PROJECT_ROOT) / "build" / "reports" / report_name
    if not os.path.lexists(path):
        return SourceSummary(source, "not-started")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        candidates = payload["candidates"]
        if payload.get("version") != 1 or not isinstance(candidates, list):
            raise ValueError("unsupported report")
        statuses = []
        for candidate in candidates:
            status = candidate.get("status") if isinstance(candidate, dict) else None
            if status not in _CANDIDATE_STATUSES:
                raise ValueError("unsupported candidate")
            statuses.append(status)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return SourceSummary(source, "attention", 0, 1)
    actionable = sum(status in {"conflict", "blocked"} for status in statuses)
    return SourceSummary(
        source,
        "degraded" if actionable else "ready",
        len(statuses),
        actionable,
    )


def load_build_summary() -> BuildSummary:
    path = Path(PROJECT_ROOT) / "build" / "reports" / "writings.json"
    if not os.path.lexists(path):
        return BuildSummary("not-started")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        counts = payload["counts"]
        required = {"published", "retained", "skipped", "removed", "issues"}
        if (
            payload.get("version") != 1
            or payload.get("status") not in {"ok", "degraded"}
            or not isinstance(counts, dict)
            or set(counts) != required
            or any(type(counts[key]) is not int or counts[key] < 0 for key in required)
        ):
            raise ValueError("unsupported report")
        return BuildSummary(
            "degraded" if payload["status"] == "degraded" or counts["issues"] else "success",
            counts["published"],
            counts["retained"],
            counts["skipped"],
            counts["removed"],
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return BuildSummary("failed")


def _draft_records() -> tuple[DraftRecord, ...]:
    root = draft_root()
    if not root.is_dir():
        return ()
    try:
        reviews = load_reviews()
    except WorkbenchError:
        reviews = {}
    records: list[DraftRecord] = []
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return (DraftRecord("private-drafts", "attention"),)
    for entry in entries:
        if not entry.is_dir():
            records.append(DraftRecord(entry.name, "attention"))
            continue
        try:
            validate_writing_bundle(entry)
            fingerprint = fingerprint_bundle(entry)
        except (WritingCatalogError, WritingImportError, OSError):
            records.append(DraftRecord(entry.name, "draft"))
            continue
        review = reviews.get(entry.name)
        records.append(
            DraftRecord(
                entry.name,
                "ready"
                if review and review.get("preview_fingerprint") == fingerprint
                else "draft",
            )
        )
    return tuple(records)


def collect_status() -> WorkbenchStatus:
    drafts = _draft_records()
    original_actionable = sum(record.status in {"draft", "conflict", "attention"} for record in drafts)
    original = SourceSummary(
        "original",
        "not-started" if not drafts else ("degraded" if original_actionable else "ready"),
        len(drafts),
        original_actionable,
    )
    return WorkbenchStatus(
        drafts,
        (
            original,
            _adapter_summary("notion", "notion-import.json"),
            _adapter_summary("weread", "weread-import.json"),
        ),
        load_build_summary(),
    )


def serialize_status(status: WorkbenchStatus) -> str:
    payload = {
        "version": 1,
        "sources": [asdict(source) for source in status.sources],
        "drafts": [
            {"slug": record.slug, "status": record.status}
            for record in status.drafts
        ],
        "build": asdict(status.build),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def render_status(status: WorkbenchStatus) -> str:
    lines = [
        "Local writing status: "
        + "; ".join(
            f"{source.source}={source.status}({source.total}, action={source.actionable})"
            for source in status.sources
        ),
        (
            "Public build: "
            f"{status.build.status}; published={status.build.published} "
            f"retained={status.build.retained} skipped={status.build.skipped} "
            f"removed={status.build.removed}"
        ),
    ]
    lines.extend(
        f"Draft {record.slug}: {record.status}; edit and preview before apply."
        for record in status.drafts
        if record.status != "ready"
    )
    return "\n".join(lines) + "\n"
