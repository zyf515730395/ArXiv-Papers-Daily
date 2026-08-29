"""Persist cloud-collected arXiv candidates and apply local curation decisions."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
from typing import Any

from paper_summarizer import PaperCandidate, enqueue_candidates, load_state


LEDGER_VERSION = 1
ARXIV_VERSION = re.compile(r"v\d+$")
VALID_STATUSES = {"pending", "accepted", "rejected"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def normalize_arxiv_id(paper_id: str) -> str:
    return ARXIV_VERSION.sub("", paper_id.strip())


def empty_ledger() -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "updated_at": None, "papers": {}}


def load_candidate_ledger(path: str | Path) -> dict[str, Any]:
    ledger_path = Path(path)
    if not ledger_path.exists():
        return empty_ledger()
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid candidate ledger: {ledger_path}") from error
    if ledger.get("version") != LEDGER_VERSION or not isinstance(
        ledger.get("papers"), dict
    ):
        raise ValueError(f"Unsupported candidate ledger schema: {ledger_path}")
    for paper_id, entry in ledger["papers"].items():
        if not isinstance(entry, dict) or entry.get("status") not in VALID_STATUSES:
            raise ValueError(f"Invalid candidate ledger entry: {paper_id}")
    return ledger


def atomic_write_json(
    path: str | Path, payload: dict[str, Any], *, pretty: bool = True
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    content = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=pretty,
    )
    temporary.write_text(content + ("\n" if pretty else ""), encoding="utf-8")
    os.replace(temporary, destination)


def archive_ids(archive: dict[str, dict[str, object]]) -> set[str]:
    return {
        normalize_arxiv_id(paper_id)
        for entries in archive.values()
        for paper_id in entries
    }


def merge_collected_candidates(
    archive: dict[str, dict[str, object]],
    ledger: dict[str, Any],
    paper_records: list[dict[str, Any]],
    topics: list[str],
) -> tuple[dict[str, dict[str, object]], dict[str, Any], int]:
    """Merge fetched records while respecting prior accepted/rejected decisions."""
    next_archive = copy.deepcopy(archive)
    for topic in topics:
        next_archive.setdefault(topic, {})
    next_ledger = copy.deepcopy(ledger)
    papers = next_ledger["papers"]
    existing_ids = archive_ids(archive)
    collected_at = utc_now()
    added = 0

    merged: dict[str, dict[str, Any]] = {}
    for record in paper_records:
        paper_id = normalize_arxiv_id(record["id"])
        item = merged.setdefault(
            paper_id,
            {
                **record,
                "id": paper_id,
                "matched_topics": [],
                "archive_rows": {},
            },
        )
        topic = record["topic"]
        if topic not in item["matched_topics"]:
            item["matched_topics"].append(topic)
        item["archive_rows"][topic] = record["archive_row"]

    for paper_id, record in merged.items():
        entry = papers.get(paper_id)
        if entry is None and paper_id in existing_ids:
            continue
        if entry is None:
            entry = {
                "id": paper_id,
                "title": record["title"],
                "abstract": record["abstract"],
                "authors": record["authors"],
                "updated": record["updated"],
                "paper_url": record["paper_url"],
                "pdf_url": record["pdf_url"],
                "matched_topics": [],
                "archive_rows": {},
                "status": "pending",
                "selected_topic": None,
                "decision_reason": None,
                "collected_at": collected_at,
                "reviewed_at": None,
            }
            papers[paper_id] = entry
            added += 1

        entry["title"] = record["title"]
        entry["abstract"] = record["abstract"]
        entry["authors"] = record["authors"]
        entry["updated"] = record["updated"]
        entry["paper_url"] = record["paper_url"]
        entry["pdf_url"] = record["pdf_url"]
        entry["matched_topics"] = sorted(
            set(entry.get("matched_topics", [])) | set(record["matched_topics"])
        )
        entry["archive_rows"] = {
            **entry.get("archive_rows", {}),
            **record["archive_rows"],
        }

        if entry["status"] != "pending":
            continue
        for topic, archive_row in record["archive_rows"].items():
            next_archive[topic][paper_id] = archive_row

    next_ledger["updated_at"] = collected_at
    return next_archive, next_ledger, added


def load_curation_decisions(path: str | Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid curation decisions: {path}") from error
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("Curation decisions must contain a decisions list")
    return decisions


def apply_curation_decisions(
    archive: dict[str, dict[str, object]],
    ledger: dict[str, Any],
    decisions: list[dict[str, str]],
    topics: list[str],
    notes_root: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, Any], dict[str, int]]:
    """Apply a validated decision batch and enqueue accepted papers locally."""
    topic_set = set(topics)
    seen: set[str] = set()
    normalized: list[tuple[str, str, str | None, str]] = []
    papers = ledger["papers"]

    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("Each curation decision must be an object")
        paper_id = normalize_arxiv_id(str(decision.get("id", "")))
        action = str(decision.get("action", ""))
        selected_topic = decision.get("topic")
        reason = str(decision.get("reason", "")).strip()
        if not paper_id or paper_id in seen:
            raise ValueError(f"Duplicate or empty curation ID: {paper_id}")
        if paper_id not in papers:
            raise ValueError(f"Unknown curation ID: {paper_id}")
        target_status = "accepted" if action == "accept" else "rejected"
        if papers[paper_id].get("status") not in {"pending", target_status}:
            raise ValueError(f"Candidate {paper_id} has already been reviewed")
        if action not in {"accept", "reject"}:
            raise ValueError(f"Invalid curation action for {paper_id}: {action}")
        if action == "accept" and selected_topic not in topic_set:
            raise ValueError(f"Invalid selected topic for {paper_id}: {selected_topic}")
        if action == "reject" and selected_topic is not None:
            raise ValueError(f"Rejected candidate {paper_id} must not select a topic")
        seen.add(paper_id)
        normalized.append((paper_id, action, selected_topic, reason))

    next_archive = copy.deepcopy(archive)
    next_ledger = copy.deepcopy(ledger)
    accepted_candidates: list[PaperCandidate] = []
    reviewed_at = utc_now()
    accepted = rejected = 0

    for paper_id, action, selected_topic, reason in normalized:
        for topic in topics:
            next_archive.setdefault(topic, {}).pop(paper_id, None)
        entry = next_ledger["papers"][paper_id]
        entry["reviewed_at"] = reviewed_at
        entry["decision_reason"] = reason or None
        if action == "reject":
            entry["status"] = "rejected"
            entry["selected_topic"] = None
            rejected += 1
            continue

        assert selected_topic is not None
        archive_row = entry.get("archive_rows", {}).get(selected_topic)
        if not archive_row:
            archive_row = next(iter(entry.get("archive_rows", {}).values()), None)
        if not archive_row:
            raise ValueError(f"Candidate {paper_id} has no archive row")
        next_archive[selected_topic][paper_id] = archive_row
        entry["status"] = "accepted"
        entry["selected_topic"] = selected_topic
        accepted_candidates.append(
            PaperCandidate(
                paper_id=paper_id,
                title=entry["title"],
                abstract=entry["abstract"],
                paper_url=entry["paper_url"],
                pdf_url=entry["pdf_url"],
                topics=[selected_topic],
                source="new",
                archive_month=str(entry["updated"])[:7],
                archive_date=str(entry["updated"])[:10],
            )
        )
        accepted += 1

    summary_state = load_state(notes_root)
    queued = enqueue_candidates(notes_root, summary_state, accepted_candidates)
    next_ledger["updated_at"] = reviewed_at
    return next_archive, next_ledger, {
        "accepted": accepted,
        "rejected": rejected,
        "queued": queued,
    }


def candidate_status_catalog(ledger: dict[str, Any]) -> dict[str, str]:
    return {
        normalize_arxiv_id(paper_id): entry["status"]
        for paper_id, entry in ledger.get("papers", {}).items()
        if entry.get("status") in {"pending", "accepted"}
    }
