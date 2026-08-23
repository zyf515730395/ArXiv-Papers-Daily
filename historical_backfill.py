"""Select deterministic historical summary batches from the archive."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


ARCHIVE_ROW = re.compile(
    r"^\|\*\*(?P<date>\d{4}-\d{2}-\d{2})\*\*\|\*\*(?P<title>.*?)\*\*\|",
    re.DOTALL,
)
ARXIV_VERSION = re.compile(r"v\d+$")


@dataclass(frozen=True)
class HistoricalPaper:
    paper_id: str
    title: str
    updated: str
    topic: str

    @property
    def month(self) -> str:
        return self.updated[:7]


@dataclass(frozen=True)
class HistoricalBatch:
    month: str
    topic: str
    papers: tuple[HistoricalPaper, ...]
    remaining_in_bucket: int


def normalize_arxiv_id(paper_id: str) -> str:
    return ARXIV_VERSION.sub("", paper_id.strip())


def parse_archive_paper(
    topic: str, paper_id: str, archive_row: object
) -> HistoricalPaper | None:
    match = ARCHIVE_ROW.match(str(archive_row).strip())
    if match is None:
        return None
    return HistoricalPaper(
        paper_id=normalize_arxiv_id(paper_id),
        title=match.group("title").strip(),
        updated=match.group("date"),
        topic=topic,
    )


def select_historical_batch(
    archive: dict[str, dict[str, object]],
    summary_state: dict[str, Any],
    topic_order: list[str],
    limit: int,
) -> HistoricalBatch | None:
    """Return the first incomplete month/topic bucket in newest-first order."""
    if limit < 1:
        raise ValueError("Historical backfill limit must be positive")

    papers_by_bucket: dict[tuple[str, str], list[HistoricalPaper]] = {}
    months = set()
    for topic in topic_order:
        for paper_id, archive_row in archive.get(topic, {}).items():
            paper = parse_archive_paper(topic, paper_id, archive_row)
            if paper is None:
                continue
            months.add(paper.month)
            papers_by_bucket.setdefault((paper.month, topic), []).append(paper)

    state_papers = summary_state.get("papers", {})
    for month in sorted(months, reverse=True):
        for topic in topic_order:
            bucket = papers_by_bucket.get((month, topic), [])
            missing = [
                paper
                for paper in bucket
                if topic not in state_papers.get(paper.paper_id, {}).get("topics", [])
            ]
            if not missing:
                continue
            missing.sort(key=lambda paper: (paper.updated, paper.paper_id), reverse=True)
            selected = tuple(missing[:limit])
            return HistoricalBatch(
                month=month,
                topic=topic,
                papers=selected,
                remaining_in_bucket=len(missing) - len(selected),
            )
    return None
