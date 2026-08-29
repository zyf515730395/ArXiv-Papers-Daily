import json
import arxiv
import yaml
import logging
import argparse
import os
from pathlib import Path
import requests
import time

from candidate_ledger import (
    apply_curation_decisions,
    atomic_write_json as atomic_write_candidate_json,
    load_candidate_ledger,
    load_curation_decisions,
    merge_collected_candidates,
)
from historical_backfill import HistoricalBatch, select_historical_batch
from paper_summarizer import (
    PaperCandidate,
    enqueue_candidates,
    load_state,
    process_summary_queue,
    publish_summaries,
)
from site_generator import generate_site

logging.basicConfig(format='[%(asctime)s %(levelname)s] %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)

base_url = "https://arxiv.paperswithcode.com/api/v0/papers/"
arxiv_url = "http://arxiv.org/"
request_timeout = 15
arxiv_request_delay_seconds = 10.0
arxiv_retry_attempts = 4
arxiv_retry_backoff_seconds = 30
arxiv_connect_timeout_seconds = 10
arxiv_read_timeout_seconds = 60
retryable_arxiv_statuses = {429, 500, 502, 503, 504}


class ArxivRetryExhausted(RuntimeError):
    """Raised after a transient arXiv API failure exhausts its backoff."""


class ArxivTimeoutSession(requests.Session):
    """Apply finite connect/read timeouts to the arxiv package's private session."""

    def request(self, method, url, **kwargs):
        kwargs.setdefault(
            "timeout",
            (arxiv_connect_timeout_seconds, arxiv_read_timeout_seconds),
        )
        return super().request(method, url, **kwargs)


def make_arxiv_client(page_size):
    client = arxiv.Client(
        page_size=page_size,
        delay_seconds=arxiv_request_delay_seconds,
        num_retries=0,
    )
    client._session = ArxivTimeoutSession()
    return client


def is_retryable_arxiv_error(error: Exception) -> bool:
    if isinstance(error, arxiv.HTTPError):
        return error.status in retryable_arxiv_statuses
    return isinstance(
        error,
        (arxiv.UnexpectedEmptyPageError, requests.ConnectionError, requests.Timeout),
    )


def fetch_arxiv_results(client, search, topic):
    """Fetch one topic with exponential backoff for transient API failures."""
    for attempt in range(1, arxiv_retry_attempts + 1):
        try:
            return list(client.results(search))
        except (arxiv.ArxivError, requests.RequestException) as error:
            if not is_retryable_arxiv_error(error):
                raise
            if attempt == arxiv_retry_attempts:
                raise ArxivRetryExhausted(
                    f"arXiv request for {topic!r} failed after "
                    f"{arxiv_retry_attempts} attempts: {error}"
                ) from error

            wait_seconds = arxiv_retry_backoff_seconds * (2 ** (attempt - 1))
            logging.warning(
                "Transient arXiv error for %s (attempt %d/%d): %s; "
                "retrying in %d seconds",
                topic,
                attempt,
                arxiv_retry_attempts,
                error,
                wait_seconds,
            )
            time.sleep(wait_seconds)

def format_query_term(term: str) -> str:
    """Quote multi-word arXiv search terms while preserving single tokens."""
    escaped = term.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"' if len(term.split()) > 1 else escaped


def build_filter_query(
    filters: list[str],
    fields: list[str] | None = None,
    categories: list[str] | None = None,
) -> str:
    """Build a backward-compatible arXiv query with optional field/category scope."""
    if not filters:
        raise ValueError("Keyword filters must not be empty")

    if fields:
        scoped_terms = []
        for filter_term in filters:
            formatted_term = format_query_term(filter_term)
            field_query = " OR ".join(
                f"{field}:{formatted_term}" for field in fields
            )
            scoped_terms.append(f"({field_query})")
        filter_query = f'({" OR ".join(scoped_terms)})'
    else:
        filter_query = " OR ".join(format_query_term(term) for term in filters)

    if not categories:
        return filter_query

    category_query = " OR ".join(f"cat:{category}" for category in categories)
    return f"{filter_query} AND ({category_query})"


def load_config(config_file:str) -> dict:
    '''
    config_file: input config file path
    return: a dict of configuration
    '''
    def pretty_filters(**config) -> dict:
        return {
            topic: build_filter_query(
                settings['filters'],
                fields=settings.get('fields'),
                categories=settings.get('categories'),
            )
            for topic, settings in config['keywords'].items()
        }
    with open(config_file, 'r', encoding='utf-8') as f:
        config = yaml.load(f,Loader=yaml.FullLoader)
        config['kv'] = pretty_filters(**config)
        logging.info(f'config = {config}')
    return config

def get_official_code_url(paper_id: str) -> str | None:
    """Return the optional official repository without failing paper ingestion."""
    try:
        response = requests.get(base_url + paper_id, timeout=request_timeout)
        response.raise_for_status()
        paper_metadata = response.json()
    except (requests.RequestException, ValueError) as error:
        logging.warning("Code metadata unavailable for %s: %s", paper_id, error)
        return None

    official = paper_metadata.get("official")
    return official.get("url") if official else None


def get_daily_papers(
    topic,
    query="slam",
    max_results=2,
    client=None,
    paper_records=None,
    include_code=True,
):
    """
    @param topic: str
    @param query: str
    @return paper_with_code: dict
    """
    # output
    content = dict()
    search_engine = arxiv.Search(
        query = query,
        max_results = max_results,
        sort_by = arxiv.SortCriterion.SubmittedDate,
        sort_order = arxiv.SortOrder.Descending,
    )
    if client is None:
        client = make_arxiv_client(min(max(max_results, 1), 100))

    for result in fetch_arxiv_results(client, search_engine, topic):

        paper_id            = result.get_short_id()
        paper_title         = result.title
        paper_first_author  = result.authors[0]
        update_time         = result.updated.date()

        logging.info(f"Time = {update_time} title = {paper_title} author = {paper_first_author}")

        # eg: 2108.09112v1 -> 2108.09112
        ver_pos = paper_id.find('v')
        if ver_pos == -1:
            paper_key = paper_id
        else:
            paper_key = paper_id[0:ver_pos]
        paper_url = arxiv_url + 'abs/' + paper_key
        repo_url = get_official_code_url(paper_id) if include_code else None
        if repo_url is not None:
            archive_row = "|**{}**|**{}**|{} et.al.|[{}]({})|**[link]({})**|\n".format(
                   update_time,paper_title,paper_first_author,paper_key,paper_url,repo_url)
        else:
            archive_row = "|**{}**|**{}**|{} et.al.|[{}]({})|null|\n".format(
                   update_time,paper_title,paper_first_author,paper_key,paper_url)
        content[paper_key] = archive_row
        if paper_records is not None:
            paper_records.append({
                "id": paper_key,
                "title": paper_title,
                "abstract": result.summary,
                "authors": f"{paper_first_author} et.al.",
                "updated": update_time.isoformat(),
                "paper_url": paper_url.replace("http://", "https://"),
                "pdf_url": result.pdf_url.replace("http://", "https://"),
                "topic": topic,
                "archive_row": archive_row,
            })

    return {topic:content}

def update_paper_links(filename):
    '''
    weekly update paper links in json file
    '''
    with open(filename, "r", encoding="utf-8") as f:
        content = f.read()
        if not content:
            m = {}
        else:
            m = json.loads(content)

        json_data = m.copy()

        for keywords,v in json_data.items():
            logging.info(f'keywords = {keywords}')
            for paper_id,contents in v.items():
                contents = str(contents)
                if '|null|' not in contents:
                    continue
                try:
                    repo_url = get_official_code_url(paper_id)
                    if repo_url is not None:
                        new_cont = contents.replace('|null|',f'|**[link]({repo_url})**|')
                        logging.info(f'ID = {paper_id}, contents = {new_cont}')
                        json_data[keywords][paper_id] = str(new_cont)

                except Exception as e:
                    logging.error(f"exception: {e} with id: {paper_id}")
        # dump to json file
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(json_data, f)

def update_json_file(filename, data_dict, allowed_topics=None):
    '''
    daily update json file using data_dict
    '''
    with open(filename, "r", encoding="utf-8") as f:
        content = f.read()
        if not content:
            m = {}
        else:
            m = json.loads(content)

    if allowed_topics is None:
        json_data = m.copy()
    else:
        json_data = {topic: m.get(topic, {}) for topic in allowed_topics}

    # update papers in each keywords
    for data in data_dict:
        for keyword in data.keys():
            papers = data[keyword]

            if keyword in json_data.keys():
                json_data[keyword].update(papers)
            else:
                json_data[keyword] = papers

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(json_data, f)

def load_archive(filename):
    with open(filename, "r", encoding="utf-8") as f:
        content = f.read()
    return json.loads(content) if content else {}


def new_summary_candidates(existing_archive, paper_records):
    """Merge newly discovered records so each arXiv ID is summarized once."""
    new_ids = {
        record["id"]
        for record in paper_records
        if record["id"] not in existing_archive.get(record["topic"], {})
    }
    merged = {}
    for record in paper_records:
        if record["id"] not in new_ids:
            continue
        if record["id"] not in merged:
            merged[record["id"]] = PaperCandidate(
                paper_id=record["id"],
                title=record["title"],
                abstract=record["abstract"],
                paper_url=record["paper_url"],
                pdf_url=record["pdf_url"],
                topics=[record["topic"]],
            )
        else:
            merged[record["id"]].topics.append(record["topic"])
    return list(merged.values())


def fetch_all_topics(keywords, max_results, include_code=True):
    """Fetch every configured topic while preserving partial successful results."""
    data_collector = []
    paper_records = []
    arxiv_client = make_arxiv_client(min(max(max_results, 1), 100))
    failed_topics = []
    for topic, keyword in keywords.items():
        logging.info("Keyword: %s", topic)
        try:
            data = get_daily_papers(
                topic,
                query=keyword,
                max_results=max_results,
                client=arxiv_client,
                paper_records=paper_records,
                include_code=include_code,
            )
        except ArxivRetryExhausted as error:
            logging.error("Skipping topic after retries: %s", error)
            failed_topics.append((topic, error))
            continue
        data_collector.append(data)
        print("\n")

    if failed_topics and not data_collector:
        failed_names = ", ".join(topic for topic, _ in failed_topics)
        raise RuntimeError(
            f"All arXiv topics failed after retries: {failed_names}"
        ) from failed_topics[-1][1]
    if failed_topics:
        logging.warning(
            "Completed with stale data preserved for failed topics: %s",
            ", ".join(topic for topic, _ in failed_topics),
        )
    return data_collector, paper_records, failed_topics


def env_flag(name, default=False):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def positive_env_int(name, default):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def optional_positive_env_int(name):
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return None
    return positive_env_int(name, None)


def historical_summary_candidates(batch: HistoricalBatch, summary_state, client):
    """Resolve archive-only rows to full arXiv metadata for one ordered batch."""
    candidates = []
    missing_ids = []
    state_papers = summary_state["papers"]

    for paper in batch.papers:
        entry = state_papers.get(paper.paper_id)
        if entry is None:
            missing_ids.append(paper.paper_id)
            continue
        candidates.append(PaperCandidate(
            paper_id=paper.paper_id,
            title=entry["title"],
            abstract=entry.get("abstract", ""),
            paper_url=entry["paper_url"],
            pdf_url=entry["pdf_url"],
            topics=[batch.topic],
            source="historical",
            archive_month=batch.month,
            archive_date=paper.updated,
        ))

    if not missing_ids:
        return candidates, set()

    archive_by_id = {paper.paper_id: paper for paper in batch.papers}

    def archive_fallback(paper_id):
        archive_paper = archive_by_id[paper_id]
        return PaperCandidate(
            paper_id=paper_id,
            title=archive_paper.title,
            abstract="",
            paper_url=f"https://arxiv.org/abs/{paper_id}",
            pdf_url=f"https://arxiv.org/pdf/{paper_id}.pdf",
            topics=[batch.topic],
            source="historical",
            archive_month=batch.month,
            archive_date=archive_paper.updated,
        )

    if not env_flag("SUMMARY_HISTORY_METADATA_LOOKUP", True):
        logging.info(
            "Using archive metadata and direct PDF URLs for %d historical papers",
            len(missing_ids),
        )
        candidates.extend(archive_fallback(paper_id) for paper_id in missing_ids)
        return candidates, set()

    search = arxiv.Search(id_list=missing_ids, max_results=len(missing_ids))
    try:
        results = fetch_arxiv_results(
            client,
            search,
            f"historical backfill {batch.month}/{batch.topic}",
        )
    except (ArxivRetryExhausted, arxiv.ArxivError, requests.RequestException) as error:
        logging.error(
            "Historical metadata unavailable for %s/%s; using archive fallback: %s",
            batch.month,
            batch.topic,
            error,
        )
        candidates.extend(archive_fallback(paper_id) for paper_id in missing_ids)
        return candidates, set()

    results_by_id = {}
    for result in results:
        paper_id = result.get_short_id().split("v", 1)[0]
        results_by_id[paper_id] = result

    for paper_id in missing_ids:
        result = results_by_id.get(paper_id)
        if result is None:
            logging.warning(
                "Historical arXiv metadata missing for %s; using archive fallback",
                paper_id,
            )
            candidates.append(archive_fallback(paper_id))
            continue
        archive_paper = archive_by_id[paper_id]
        pdf_url = result.pdf_url or f"https://arxiv.org/pdf/{paper_id}.pdf"
        candidates.append(PaperCandidate(
            paper_id=paper_id,
            title=result.title or archive_paper.title,
            abstract=result.summary,
            paper_url=f"https://arxiv.org/abs/{paper_id}",
            pdf_url=pdf_url.replace("http://", "https://"),
            topics=[batch.topic],
            source="historical",
            archive_month=batch.month,
            archive_date=archive_paper.updated,
        ))
    resolved_ids = {candidate.paper_id for candidate in candidates}
    return candidates, set(missing_ids) - resolved_ids


def queue_historical_backfill(
    archive,
    summary_state,
    notes_root,
    topics,
    limit,
    client,
    target_year=None,
    excluded_ids=None,
):
    batch = select_historical_batch(
        archive,
        summary_state,
        topics,
        limit,
        target_year=target_year,
        excluded_ids=excluded_ids,
    )
    if batch is None:
        return None, 0, set()

    candidates, unresolved_ids = historical_summary_candidates(
        batch, summary_state, client
    )
    enqueue_candidates(notes_root, summary_state, candidates)
    logging.info(
        "Historical backfill bucket %s/%s selected=%d resolved=%d remaining=%d",
        batch.month,
        batch.topic,
        len(batch.papers),
        len(candidates),
        batch.remaining_in_bucket,
    )
    return batch, len(candidates), unresolved_ids


def count_summary_work(state, target_year=None):
    """Count pending or stale summaries within the requested historical scope."""
    remaining = 0
    for entry in state["papers"].values():
        if entry.get("status") == "ready" and not entry.get("needs_refresh"):
            continue
        if entry.get("source") == "historical" and target_year is not None:
            archive_value = entry.get("archive_date") or entry.get("archive_month") or ""
            if not str(archive_value).startswith(f"{target_year:04d}-"):
                continue
        remaining += 1
    return remaining


def run_historical_backfill(
    archive,
    notes_root,
    publish_root,
    topics,
    limit,
    client,
    target_year=None,
    budget_minutes=None,
    base_url="http://127.0.0.1:8000/v1",
    model_override=None,
    publish=True,
):
    """Process ordered historical batches, optionally stopping at a deadline."""
    deadline = (
        time.monotonic() + budget_minutes * 60
        if budget_minutes is not None
        else None
    )
    attempted_ids = set()
    totals = {"completed": 0, "failed": 0, "attempted": 0}
    stop_reason = "complete"

    while True:
        stats = process_summary_queue(
            notes_root,
            publish_root,
            base_url,
            model_override,
            topics,
            deadline=deadline,
            attempted_ids=attempted_ids,
            include_new=True,
            include_historical=True,
            historical_year=target_year,
            publish=False,
        )
        for key in totals:
            totals[key] += int(stats[key])

        if stats["blocked"]:
            stop_reason = "model-unavailable"
            break
        if stats["budget_exhausted"] or (
            deadline is not None and time.monotonic() >= deadline
        ):
            stop_reason = "time-budget"
            break

        summary_state = load_state(notes_root)
        batch, resolved, unresolved_ids = queue_historical_backfill(
            archive,
            summary_state,
            notes_root,
            topics,
            limit,
            client,
            target_year=target_year,
            excluded_ids=attempted_ids,
        )
        if batch is None:
            remaining = select_historical_batch(
                archive,
                summary_state,
                topics,
                1,
                target_year=target_year,
            )
            pending = count_summary_work(summary_state, target_year)
            stop_reason = (
                "complete"
                if remaining is None and pending == 0
                else "deferred-failures"
            )
            break
        attempted_ids.update(unresolved_ids)
        if resolved == 0:
            logging.warning(
                "Deferring %d papers with unavailable metadata and continuing",
                len(unresolved_ids),
            )
            continue

    final_state = load_state(notes_root)
    if publish:
        publish_summaries(notes_root, final_state, publish_root, topics)
    remaining = select_historical_batch(
        archive,
        final_state,
        topics,
        1,
        target_year=target_year,
    )
    pending = count_summary_work(final_state, target_year)
    totals.update({
        "pending": pending,
        "backfill_complete": remaining is None and pending == 0,
        "stop_reason": stop_reason,
    })
    logging.info(
        "Historical backfill result year=%s budget=%s result=%s",
        target_year if target_year is not None else "all",
        f"{budget_minutes}m" if budget_minutes is not None else "unlimited",
        totals,
    )
    return totals


def demo(**config):
    keywords = config['kv']
    max_results = config['max_results']
    json_file = config['json_gitpage_path']
    html_file = config['html_gitpage_path']
    candidate_file = config.get('candidate_ledger_path')
    candidate_storage_file = candidate_file or './data/arxiv-candidates.json'

    def generate_current_site():
        if candidate_file:
            generate_site(json_file, html_file, candidate_file)
        else:
            generate_site(json_file, html_file)

    summaries_only = config.get('summaries_only', False)
    backfill_history = config.get('backfill_history', False)
    collect_only = config.get('collect_only', False)
    curation_path = config.get('apply_curation')
    publish_only = config.get('publish_only', False)
    new_only = config.get('new_only', False)
    no_publish = config.get('no_publish', False)

    exclusive_modes = sum(
        bool(value)
        for value in (collect_only, curation_path, publish_only, summaries_only)
    )
    if exclusive_modes > 1:
        raise ValueError(
            "--collect-only, --apply-curation, --publish-only and "
            "--summaries-only are mutually exclusive"
        )
    if new_only and not summaries_only:
        raise ValueError("--new-only requires --summaries-only")
    if no_publish and not summaries_only:
        raise ValueError("--no-publish requires --summaries-only")

    if collect_only:
        logging.info("Cloud candidate collection begin")
        _, paper_records, failed_topics = fetch_all_topics(
            keywords, max_results, include_code=False
        )
        archive = load_archive(json_file)
        ledger = load_candidate_ledger(candidate_storage_file)
        next_archive, next_ledger, added = merge_collected_candidates(
            archive, ledger, paper_records, list(keywords)
        )
        atomic_write_candidate_json(json_file, next_archive, pretty=False)
        atomic_write_candidate_json(candidate_storage_file, next_ledger)
        generate_current_site()
        stats = {
            "collected": len(paper_records),
            "new_candidates": added,
            "failed_topics": len(failed_topics),
        }
        logging.info("Cloud candidate collection result = %s", stats)
        return stats

    automatic_backfill = (
        not summaries_only and env_flag("SUMMARY_BACKFILL_ENABLED")
    )
    backfill_enabled = backfill_history or automatic_backfill
    summary_enabled = (
        summaries_only
        or backfill_enabled
        or bool(curation_path)
        or publish_only
        or env_flag("SUMMARY_ENABLED")
    )
    backfill_limit = (
        positive_env_int("SUMMARY_BACKFILL_LIMIT", 10)
        if backfill_enabled
        else 10
    )
    backfill_year = (
        optional_positive_env_int("SUMMARY_BACKFILL_YEAR")
        if backfill_enabled
        else None
    )
    backfill_budget_minutes = (
        positive_env_int("SUMMARY_BACKFILL_TIME_BUDGET_MINUTES", 150)
        if backfill_enabled
        else 150
    )
    notes_root = None
    publish_root = Path(html_file).parent / "notes"
    summary_state = None
    if summary_enabled:
        notes_root = Path(os.getenv("PAPER_NOTES_ROOT", "/mnt/g/share/papers"))
        summary_state = load_state(notes_root)

    if curation_path:
        archive = load_archive(json_file)
        ledger = load_candidate_ledger(candidate_storage_file)
        decisions = load_curation_decisions(curation_path)
        next_archive, next_ledger, stats = apply_curation_decisions(
            archive,
            ledger,
            decisions,
            list(keywords),
            notes_root,
        )
        atomic_write_candidate_json(json_file, next_archive, pretty=False)
        atomic_write_candidate_json(candidate_storage_file, next_ledger)
        publish_summaries(
            notes_root,
            load_state(notes_root),
            publish_root,
            list(keywords),
        )
        generate_current_site()
        logging.info("Curation result = %s", stats)
        return stats

    if publish_only:
        catalog = publish_summaries(
            notes_root,
            summary_state,
            publish_root,
            list(keywords),
        )
        generate_current_site()
        stats = {
            "published_topics": len(catalog.get("topics", {})),
            "pending": count_summary_work(load_state(notes_root)),
        }
        logging.info("Summary publication result = %s", stats)
        return stats

    if summaries_only:
        if backfill_enabled:
            archive = load_archive(json_file)
            backfill_client = make_arxiv_client(min(backfill_limit, 100))
            stats = run_historical_backfill(
                archive,
                notes_root,
                publish_root,
                list(keywords),
                backfill_limit,
                backfill_client,
                backfill_year,
                backfill_budget_minutes,
                os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
                os.getenv("VLLM_MODEL") or None,
                publish=not no_publish,
            )
        else:
            stats = process_summary_queue(
                notes_root,
                publish_root,
                os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
                os.getenv("VLLM_MODEL") or None,
                list(keywords),
                include_new=True,
                include_historical=not new_only,
                publish=not no_publish,
            )
        logging.info("Summary queue result = %s", stats)
        if not no_publish:
            generate_current_site()
        logging.info("Summary-only update finished")
        return stats

    b_update = config['update_paper_links']
    logging.info(f'Update Paper Link = {b_update}')
    paper_records = []
    existing_archive = load_archive(json_file) if summary_enabled else {}
    if config['update_paper_links'] == False:
        logging.info(f"GET daily papers begin")
        data_collector, paper_records, _ = fetch_all_topics(
            keywords, max_results, include_code=True
        )
        logging.info(f"GET daily papers end")

    if config['update_paper_links']:
        update_paper_links(json_file)
    else:
        if summary_enabled:
            candidates = new_summary_candidates(existing_archive, paper_records)
            added = enqueue_candidates(notes_root, summary_state, candidates)
            logging.info("Queued %d newly discovered papers for summary", added)
        update_json_file(json_file, data_collector, allowed_topics=keywords)
        if summary_enabled:
            if backfill_enabled:
                backfill_client = make_arxiv_client(min(backfill_limit, 100))
                stats = run_historical_backfill(
                    load_archive(json_file),
                    notes_root,
                    publish_root,
                    list(keywords),
                    backfill_limit,
                    backfill_client,
                    backfill_year,
                    backfill_budget_minutes,
                    os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
                    os.getenv("VLLM_MODEL") or None,
                )
            elif env_flag("SUMMARY_DEFER_PROCESSING"):
                deferred_state = load_state(notes_root)
                publish_summaries(
                    notes_root,
                    deferred_state,
                    publish_root,
                    list(keywords),
                )
                stats = {
                    "completed": 0,
                    "failed": 0,
                    "attempted": 0,
                    "pending": count_summary_work(deferred_state),
                    "deferred": True,
                }
            else:
                stats = process_summary_queue(
                    notes_root,
                    publish_root,
                    os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
                    os.getenv("VLLM_MODEL") or None,
                    list(keywords),
                    include_historical=False,
                )
            logging.info("Summary queue result = %s", stats)
    generate_current_site()
    logging.info("Update GitPage finished")
    return {"updated": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', type=str, default='config.yaml',
                        help='configuration file path')
    parser.add_argument('--update_paper_links', default=False,
                        action="store_true", help='whether to update paper links etc.')
    parser.add_argument('--summaries-only', default=False, action="store_true",
                        help='process the persisted summary queue only')
    parser.add_argument('--backfill-history', default=False, action="store_true",
                        help='enqueue the next ordered historical summary batch')
    parser.add_argument('--collect-only', default=False, action="store_true",
                        help='collect candidates and publish the archive without summaries')
    parser.add_argument('--apply-curation', type=str,
                        help='apply local accept/reject decisions from JSON')
    parser.add_argument('--new-only', default=False, action="store_true",
                        help='process only non-historical pending summaries')
    parser.add_argument('--no-publish', default=False, action="store_true",
                        help='update external summary state without changing docs')
    parser.add_argument('--publish-only', default=False, action="store_true",
                        help='render external summaries without running inference')
    parser.add_argument('--result-json', type=str,
                        help='write machine-readable command results to this path')
    args = parser.parse_args()
    config = load_config(args.config_path)
    config = {
        **config,
        'update_paper_links': args.update_paper_links,
        'summaries_only': args.summaries_only,
        'backfill_history': args.backfill_history,
        'collect_only': args.collect_only,
        'apply_curation': args.apply_curation,
        'new_only': args.new_only,
        'no_publish': args.no_publish,
        'publish_only': args.publish_only,
    }
    result = demo(**config)
    if args.result_json:
        atomic_write_candidate_json(args.result_json, result or {})
