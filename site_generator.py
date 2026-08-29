"""Generate the standalone GitHub Pages paper archive."""

import calendar
from collections import OrderedDict
import datetime
import html
import json
from pathlib import Path
import re
import unicodedata

from milestone_catalog import (
    first_ready_family,
    load_milestone_catalog,
    render_primary_sidebar,
)


ENTRY_PATTERN = re.compile(
    r"^\|\*\*(?P<date>[^*]+)\*\*\|\*\*(?P<title>.*?)\*\*\|"
    r"(?P<authors>.*?)\|\[(?P<pdf_label>[^]]+)]\((?P<pdf_url>[^)]+)\)\|"
    r"(?P<code>.*?)\|$"
)
SITE_TITLE = "Arxiv Papers Daily"
RECENT_YEAR_COUNT = 3
NOTES_DIRECTORY_NAME = "notes"
SHOW_BOOK_NOTES_NAV = False
DEFAULT_MILESTONE_CATALOG = Path(__file__).with_name("milestone_models.yaml")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def parse_entry(paper_id: str, entry: str) -> dict:
    match = ENTRY_PATTERN.match(entry.strip())
    if match is None:
        raise ValueError(f"Unexpected paper entry for {paper_id}: {entry[:100]}")

    values = match.groupdict()
    published = datetime.date.fromisoformat(values["date"])
    code_match = re.search(r"\[[^]]*]\(([^)]+)\)", values["code"])
    paper_url = values["pdf_url"].replace("http://arxiv.org/", "https://arxiv.org/")
    return {
        "id": paper_id,
        "date": published,
        "title": values["title"],
        "authors": values["authors"],
        "paper_url": paper_url,
        "code_url": code_match.group(1) if code_match else None,
    }


def build_archive(data: dict) -> tuple[list[dict], OrderedDict]:
    categories = []
    themes = OrderedDict()

    for topic, entries in data.items():
        rows = [parse_entry(paper_id, entry) for paper_id, entry in entries.items()]
        rows.sort(key=lambda row: (row["date"], row["id"]), reverse=True)

        grouped_years = {}
        for row in rows:
            week_start, _ = week_bounds(row["date"])
            if week_start.year == row["date"].year:
                year = week_start.year
                month = week_start.month
            else:
                # Keep January papers in their publication year when a natural
                # week starts in the previous December.
                year = row["date"].year
                month = row["date"].month
            grouped_years.setdefault(year, {}).setdefault(month, {}).setdefault(
                week_start, []
            ).append(row)

        years = OrderedDict()
        for year in sorted(grouped_years, reverse=True):
            months = OrderedDict()
            for month in sorted(grouped_years[year], reverse=True):
                weeks = grouped_years[year][month]
                months[month] = OrderedDict(
                    (week_start, weeks[week_start])
                    for week_start in sorted(weeks, reverse=True)
                )
            years[year] = months

        category = {
            "topic": topic,
            "theme": topic,
            "subtype": None,
            "slug": slugify(topic),
            "count": len(rows),
            "years": years,
        }
        categories.append(category)
        themes.setdefault(topic, []).append(category)

    return categories, themes


def month_anchor(category: dict, year: int, month: int) -> str:
    return f'{category["slug"]}-{year}-{month:02d}'


def week_anchor(category: dict, year: int, month: int, week_start: datetime.date) -> str:
    return f'{month_anchor(category, year, month)}-week-{week_start.isoformat()}'


def week_bounds(published: datetime.date) -> tuple[datetime.date, datetime.date]:
    week_start = published - datetime.timedelta(days=published.weekday())
    return week_start, week_start + datetime.timedelta(days=6)


def week_label(week_start: datetime.date) -> str:
    week_end = week_start + datetime.timedelta(days=6)
    start_month = calendar.month_abbr[week_start.month]
    end_month = calendar.month_abbr[week_end.month]
    if week_start.year != week_end.year:
        return (
            f"{start_month} {week_start.day}, {week_start.year}"
            f"–{end_month} {week_end.day}, {week_end.year}"
        )
    if week_start.month != week_end.month:
        return f"{start_month} {week_start.day}–{end_month} {week_end.day}"
    return f"{start_month} {week_start.day}–{week_end.day}"


def month_paper_count(weeks: OrderedDict) -> int:
    return sum(len(rows) for rows in weeks.values())


def year_paper_count(months: OrderedDict) -> int:
    return sum(month_paper_count(weeks) for weeks in months.values())


def filter_recent_archive(
    categories: list[dict], current_year: int, year_count: int = RECENT_YEAR_COUNT
) -> tuple[list[dict], OrderedDict]:
    earliest_year = current_year - year_count + 1
    recent_categories = []
    recent_themes = OrderedDict()

    for category in categories:
        years = OrderedDict(
            (year, months)
            for year, months in category["years"].items()
            if earliest_year <= year <= current_year
        )
        if not years:
            continue

        recent_category = {
            **category,
            "count": sum(year_paper_count(months) for months in years.values()),
            "years": years,
        }
        recent_categories.append(recent_category)
        recent_themes.setdefault(recent_category["theme"], []).append(recent_category)

    return recent_categories, recent_themes


def render_sidebar(
    themes: OrderedDict,
    milestone_catalog: dict,
    show_book_notes: bool = SHOW_BOOK_NOTES_NAV,
) -> str:
    milestone_family = first_ready_family(milestone_catalog)
    output = [
        '<div class="navigation-shell" id="navigation-shell">',
        *render_primary_sidebar(
            "papers",
            "#top",
            f"milestone-models/{milestone_family['slug']}.html",
        ),
        '  <aside class="paper-sidebar" id="paper-sidebar" aria-label="Paper archive">',
        '    <div class="sidebar-brand">',
        '      <a href="#top">论文阅读</a>',
        '      <span>按主题、年份与月份浏览</span>',
        '    </div>',
        '    <div class="sidebar-actions">',
        '      <button type="button" data-sidebar-action="expand">Expand all</button>',
        '      <button type="button" data-sidebar-action="collapse">Collapse all</button>',
        '    </div>',
        '    <nav class="archive-nav">',
    ]

    for theme_index, (theme, categories) in enumerate(themes.items()):
        theme_count = sum(category["count"] for category in categories)
        open_attribute = " open" if theme_index == 0 else ""
        output.append(f'    <details class="nav-theme"{open_attribute}>')
        output.append(
            f'      <summary><span>{html.escape(theme)}</span>'
            f'<span class="nav-count">{theme_count}</span></summary>'
        )

        for category_index, category in enumerate(categories):
            has_subtype = category["subtype"] is not None
            if has_subtype:
                category_open = " open" if theme_index == 0 and category_index == 0 else ""
                output.append(f'      <details class="nav-subtopic"{category_open}>')
                output.append(
                    f'        <summary><span>{html.escape(category["subtype"])}</span>'
                    f'<span class="nav-count">{category["count"]}</span></summary>'
                )

            indent = "        " if has_subtype else "      "
            for year_index, (year, months) in enumerate(category["years"].items()):
                year_count = year_paper_count(months)
                year_open = " open" if theme_index == 0 and category_index == 0 and year_index == 0 else ""
                output.append(f'{indent}<details class="nav-year"{year_open}>')
                output.append(
                    f'{indent}  <summary><span>{year}</span>'
                    f'<span class="nav-count">{year_count}</span></summary>'
                )
                output.append(f'{indent}  <ul>')
                for month, weeks in months.items():
                    anchor = month_anchor(category, year, month)
                    output.append(
                        f'{indent}    <li><a href="#{anchor}">'
                        f'<span>{calendar.month_name[month]}</span>'
                        f'<span class="nav-count">{month_paper_count(weeks)}</span></a></li>'
                    )
                output.append(f'{indent}  </ul>')
                output.append(f'{indent}</details>')

            if has_subtype:
                output.append("      </details>")

        output.append("    </details>")

    output.extend(["    </nav>", "  </aside>", "</div>"])
    return "\n".join(output)


def load_summary_catalog(output_path: str | Path) -> dict[str, dict]:
    notes_directory = Path(output_path).parent / NOTES_DIRECTORY_NAME
    if not notes_directory.is_dir():
        return {}

    catalog = {}
    manifest_pattern = re.compile(
        r'<script type="application/json" id="summary-catalog">(.*?)</script>',
        re.DOTALL,
    )
    for summary_path in sorted(notes_directory.glob("*.html")):
        document = summary_path.read_text(encoding="utf-8")
        match = manifest_pattern.search(document)
        if match is None:
            continue
        try:
            manifest = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid summary manifest: {summary_path}") from error
        topic = manifest.get("topic")
        papers = manifest.get("papers")
        if not isinstance(topic, str) or not isinstance(papers, dict):
            raise ValueError(f"Invalid summary manifest schema: {summary_path}")
        catalog[topic] = papers
    return catalog


def render_table(
    rows: list[dict],
    topic: str,
    summary_catalog: dict[str, dict],
    candidate_statuses: dict[str, str] | None = None,
) -> str:
    output = [
        '<div class="table-scroll">',
        '  <table class="paper-table">',
        '    <thead><tr><th>Arxiv ID</th><th>Paper</th><th>Authors</th><th>Summary</th></tr></thead>',
        '    <tbody>',
    ]
    topic_summaries = summary_catalog.get(topic, {})
    candidate_statuses = candidate_statuses or {}
    for row in rows:
        paper_url = html.escape(row["paper_url"], quote=True)
        summary = topic_summaries.get(row["id"], {})
        summary_cell = '<span class="muted">—</span>'
        if summary.get("status") == "pending":
            summary_cell = '<span class="summary-pending">待生成</span>'
        elif summary.get("status") == "ready" and summary.get("url"):
            summary_url = html.escape(summary["url"], quote=True)
            summary_id = html.escape(f"summary-{row['id']}", quote=True)
            summary_cell = (
                f'<a class="summary-link" href="{summary_url}" '
                f'data-summary-url="{summary_url}" data-summary-id="{summary_id}" '
                'aria-haspopup="dialog">查看要点</a>'
            )
        elif candidate_statuses.get(row["id"]) in {"pending", "accepted"}:
            summary_cell = '<span class="summary-pending">待生成</span>'
        output.append(
            "      <tr>"
            f'<td class="paper-id"><a href="{paper_url}" target="_blank" rel="noopener">'
            f'{html.escape(row["id"])}</a></td>'
            f'<td class="paper-title"><a href="{paper_url}" target="_blank" rel="noopener">'
            f'{html.escape(row["title"])}</a></td>'
            f'<td>{html.escape(row["authors"])}</td>'
            f'<td class="paper-summary">{summary_cell}</td>'
            "</tr>"
        )
    output.extend(["    </tbody>", "  </table>", "</div>"])
    return "\n".join(output)


def render_content(
    categories: list[dict],
    summary_catalog: dict[str, dict],
    candidate_statuses: dict[str, str] | None = None,
) -> str:
    output = []
    for category in categories:
        eyebrow = category["theme"]
        heading = category["subtype"] or category["theme"]
        output.extend([
            f'<section class="topic-section" id="{category["slug"]}">',
            '  <header class="topic-header">',
            f'    <p>{html.escape(eyebrow)}</p>',
            f'    <h2>{html.escape(heading)}</h2>',
            f'    <span>{category["count"]} papers</span>',
            '  </header>',
        ])
        for year_index, (year, months) in enumerate(category["years"].items()):
            year_count = year_paper_count(months)
            year_id = f'{category["slug"]}-{year}-content'
            year_expanded = "true" if year_index == 0 else "false"
            selected_month = next(iter(months))
            output.append(
                f'  <section class="archive-year" data-archive-year '
                f'data-expanded="{year_expanded}">'
            )
            output.extend([
                '    <div class="archive-year-header">',
                f'      <button class="archive-year-toggle" type="button" '
                f'aria-expanded="{year_expanded}" aria-controls="{year_id}">',
                f'        <span>{year}</span>',
                '      </button>',
                f'      <div class="archive-month-tabs" role="tablist" '
                f'aria-label="{year} months">',
            ])
            for month in range(1, 13):
                weeks = months.get(month)
                month_name = calendar.month_abbr[month]
                if weeks is None:
                    output.append(
                        f'        <button type="button" role="tab" disabled '
                        f'aria-disabled="true" aria-selected="false">{month_name}</button>'
                    )
                    continue
                anchor = month_anchor(category, year, month)
                is_selected = month == selected_month
                selected = "true" if is_selected else "false"
                tabindex = "0" if is_selected else "-1"
                output.append(
                    f'        <button id="{anchor}-tab" type="button" role="tab" '
                    f'aria-controls="{anchor}" aria-selected="{selected}" '
                    f'tabindex="{tabindex}" data-month-target="{anchor}">{month_name}</button>'
                )
            output.extend([
                '      </div>',
                f'      <span class="archive-year-count">{year_count} papers</span>',
                '    </div>',
                f'    <div class="archive-year-content" id="{year_id}">',
            ])
            for month, weeks in months.items():
                anchor = month_anchor(category, year, month)
                is_active = month == selected_month
                active = "true" if is_active else "false"
                output.append(
                    f'      <section class="archive-month-panel" id="{anchor}" '
                    f'role="tabpanel" aria-labelledby="{anchor}-tab" '
                    f'aria-hidden="{"false" if is_active else "true"}" data-active="{active}">'
                )
                for week_index, (week_start, rows) in enumerate(weeks.items()):
                    anchor_id = week_anchor(category, year, month, week_start)
                    week_open = " open" if week_index == 0 else ""
                    output.append(
                        f'        <details class="archive-week" id="{anchor_id}"{week_open}>'
                    )
                    output.append(
                        f'          <summary><span>{week_label(week_start)}</span>'
                        f'<span>{len(rows)} papers</span></summary>'
                    )
                    output.append(
                        render_table(
                            rows,
                            category["topic"],
                            summary_catalog,
                            candidate_statuses,
                        )
                    )
                    output.append("        </details>")
                output.append("      </section>")
            output.extend(["    </div>", "  </section>"])
        output.append("</section>")
    return "\n".join(output)


def load_candidate_statuses(candidate_path: str | Path | None) -> dict[str, str]:
    if candidate_path is None or not Path(candidate_path).is_file():
        return {}
    payload = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("papers"), dict):
        raise ValueError(f"Invalid candidate ledger schema: {candidate_path}")
    return {
        paper_id: entry.get("status")
        for paper_id, entry in payload["papers"].items()
        if isinstance(entry, dict) and entry.get("status") in {"pending", "accepted"}
    }


def generate_site(
    json_path: str | Path,
    output_path: str | Path,
    candidate_path: str | Path | None = None,
    milestone_catalog_path: str | Path = DEFAULT_MILESTONE_CATALOG,
) -> None:
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    milestone_catalog = load_milestone_catalog(milestone_catalog_path)
    all_categories, _ = build_archive(data)
    today = datetime.date.today()
    categories, themes = filter_recent_archive(all_categories, today.year)
    summary_catalog = load_summary_catalog(output_path)
    candidate_statuses = load_candidate_statuses(candidate_path)
    updated = today.isoformat()
    total_papers = sum(category["count"] for category in categories)
    years = {
        year
        for category in categories
        for year in category["years"]
    }

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="A daily index of image, video, and 3D generation, neural rendering, and depth estimation papers from arXiv.">
  <title>{SITE_TITLE}</title>
  <script>
    (() => {{
      document.documentElement.classList.add("js");
      const storageKey = "arxiv-theme";
      let theme = null;
      try {{ theme = window.localStorage.getItem(storageKey); }} catch (error) {{ /* Storage can be unavailable. */ }}
      if (theme !== "light" && theme !== "dark") {{
        theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }}
      document.documentElement.dataset.theme = theme;
    }})();
  </script>
  <link rel="stylesheet" href="assets/css/site.css">
  <script src="assets/js/sidebar.js" defer></script>
</head>
<body>
  <button class="sidebar-toggle" type="button" aria-controls="navigation-shell" aria-expanded="false">
    <span aria-hidden="true">&#9776;</span> Browse archive
  </button>
  <button class="theme-toggle" type="button" aria-label="Switch color theme" aria-pressed="false">
    <span data-theme-icon aria-hidden="true"></span>
  </button>
  <div class="sidebar-scrim" data-sidebar-close></div>
{render_sidebar(themes, milestone_catalog)}
  <main class="page-content" id="top">
    <header class="hero">
      <h1>{SITE_TITLE}</h1>
      <div class="hero-stats" aria-label="Archive statistics">
        <div><strong>{total_papers:,}</strong><span>Papers</span></div>
        <div><strong>{len(themes)}</strong><span>Topics</span></div>
        <div><strong>{len(years)}</strong><span>Years</span></div>
      </div>
      <p class="updated">Updated {updated}</p>
    </header>
{render_content(categories, summary_catalog, candidate_statuses)}
    <footer>Generated from arXiv metadata · Source: <a href="https://github.com/zyf515730395/arxiv-papers-daily">{SITE_TITLE}</a></footer>
  </main>
  <dialog class="summary-dialog" id="summary-dialog" aria-labelledby="summary-dialog-title">
    <div class="summary-dialog-shell">
      <header class="summary-dialog-header">
        <h2 id="summary-dialog-title">论文要点</h2>
        <button type="button" class="summary-dialog-close" data-summary-close aria-label="关闭">×</button>
      </header>
      <div class="summary-dialog-content" data-summary-content>
        <p class="muted">正在加载…</p>
      </div>
    </div>
  </dialog>
</body>
</html>
"""
    Path(output_path).write_text(document, encoding="utf-8")
