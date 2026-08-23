import json
import arxiv
import yaml
import logging
import argparse
import os
from pathlib import Path
import requests
import time

from paper_summarizer import (
    PaperCandidate,
    enqueue_candidates,
    load_state,
    process_summary_queue,
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
retryable_arxiv_statuses = {429, 500, 502, 503, 504}


class ArxivRetryExhausted(RuntimeError):
    """Raised after a transient arXiv API failure exhausts its backoff."""


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
    topic, query="slam", max_results=2, client=None, paper_records=None
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
        client = arxiv.Client(
            page_size=min(max(max_results, 1), 100),
            delay_seconds=arxiv_request_delay_seconds,
            num_retries=0,
        )

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
        if paper_records is not None:
            paper_records.append({
                "id": paper_key,
                "title": paper_title,
                "abstract": result.summary,
                "paper_url": paper_url.replace("http://", "https://"),
                "pdf_url": result.pdf_url.replace("http://", "https://"),
                "topic": topic,
            })


        repo_url = get_official_code_url(paper_id)
        if repo_url is not None:
            content[paper_key] = "|**{}**|**{}**|{} et.al.|[{}]({})|**[link]({})**|\n".format(
                   update_time,paper_title,paper_first_author,paper_key,paper_url,repo_url)
        else:
            content[paper_key] = "|**{}**|**{}**|{} et.al.|[{}]({})|null|\n".format(
                   update_time,paper_title,paper_first_author,paper_key,paper_url)

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


def demo(**config):
    data_collector = []

    keywords = config['kv']
    max_results = config['max_results']
    json_file = config['json_gitpage_path']
    html_file = config['html_gitpage_path']

    summaries_only = config.get('summaries_only', False)
    summary_enabled = summaries_only or os.getenv("SUMMARY_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    notes_root = None
    publish_root = Path(html_file).parent / "notes"
    summary_state = None
    if summary_enabled:
        notes_root = Path(os.getenv("PAPER_NOTES_ROOT", "/mnt/g/share/papers"))
        summary_state = load_state(notes_root)

    if summaries_only:
        stats = process_summary_queue(
            notes_root,
            publish_root,
            os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            os.getenv("VLLM_MODEL") or None,
            list(keywords),
        )
        logging.info("Summary queue result = %s", stats)
        generate_site(json_file, html_file)
        logging.info("Summary-only update finished")
        return

    b_update = config['update_paper_links']
    logging.info(f'Update Paper Link = {b_update}')
    paper_records = []
    existing_archive = load_archive(json_file) if summary_enabled else {}
    if config['update_paper_links'] == False:
        logging.info(f"GET daily papers begin")
        arxiv_client = arxiv.Client(
            page_size=min(max(max_results, 1), 100),
            delay_seconds=arxiv_request_delay_seconds,
            num_retries=0,
        )
        failed_topics = []
        for topic, keyword in keywords.items():
            logging.info(f"Keyword: {topic}")
            try:
                data = get_daily_papers(
                    topic,
                    query=keyword,
                    max_results=max_results,
                    client=arxiv_client,
                    paper_records=paper_records,
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
            stats = process_summary_queue(
                notes_root,
                publish_root,
                os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
                os.getenv("VLLM_MODEL") or None,
                list(keywords),
            )
            logging.info("Summary queue result = %s", stats)
    generate_site(json_file, html_file)
    logging.info("Update GitPage finished")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', type=str, default='config.yaml',
                        help='configuration file path')
    parser.add_argument('--update_paper_links', default=False,
                        action="store_true", help='whether to update paper links etc.')
    parser.add_argument('--summaries-only', default=False, action="store_true",
                        help='process the persisted summary queue only')
    args = parser.parse_args()
    config = load_config(args.config_path)
    config = {
        **config,
        'update_paper_links': args.update_paper_links,
        'summaries_only': args.summaries_only,
    }
    demo(**config)
