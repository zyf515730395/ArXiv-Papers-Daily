"""Safe technical-Markdown rendering for public writings."""

from __future__ import annotations

from collections import Counter
from datetime import date
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re
from typing import Callable, Mapping
from urllib.parse import urlsplit
import unicodedata
from xml.etree.ElementTree import Element

import bleach
from latex2mathml.converter import convert as convert_latex
import markdown
from markdown.extensions.toc import TocExtension
from markdown.treeprocessors import Treeprocessor

from shared.site_shell import render_section_intro, render_site_page

from .models import AssetCopy, ManifestArticle, RenderedArticle, TocEntry, WritingArticle


PROTECTED_PREFIX = "TOGOSPROTECTEDTOKEN"
BODY_H1_PATTERN = re.compile(r"^\s{0,3}#\s+", re.MULTILINE)
FENCE_OPEN_PATTERN = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[^\r\n]*(?:\r?\n|$)", re.MULTILINE)
INLINE_CODE_PATTERN = re.compile(r"(?<!`)(?P<fence>`+)(?!`)(?P<body>.*?)(?<!`)(?P=fence)(?!`)", re.DOTALL)
CODE_CLASS_PATTERN = re.compile(r"^language-[A-Za-z0-9_+.-]+$")
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
KIND_LABELS = {"learning-note": "学习笔记", "book-note": "读书笔记"}
ALLOWED_TAGS = {
    "p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote",
    "pre", "code", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "a", "img", "hr", "br", "em", "strong",
}


class WritingRenderError(ValueError):
    """A safe, stable rendering error suitable for publication reports."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _RemoveHeadingIds(Treeprocessor):
    """Ensure author attr-list IDs cannot bypass the deterministic slugifier."""

    def run(self, root: Element) -> None:
        for element in root.iter():
            if element.tag in {"h2", "h3"}:
                element.attrib.pop("id", None)


def _protect_matches(text: str, pattern: re.Pattern[str], protected: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        token = f"{PROTECTED_PREFIX}CODE{len(protected)}END"
        protected.append(match.group(0))
        return token

    return pattern.sub(replace, text)


def _protect_fenced_code(text: str, protected: list[str]) -> str:
    output: list[str] = []
    cursor = 0
    while opening := FENCE_OPEN_PATTERN.search(text, cursor):
        fence = opening.group("fence")
        closing_pattern = re.compile(
            rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*(?:\r?\n|$)",
            re.MULTILINE,
        )
        closing = closing_pattern.search(text, opening.end())
        if closing is None:
            output.append(text[cursor:opening.end()])
            cursor = opening.end()
            continue
        output.append(text[cursor:opening.start()])
        token = f"{PROTECTED_PREFIX}CODE{len(protected)}END"
        protected.append(text[opening.start():closing.end()])
        output.append(token)
        cursor = closing.end()
    output.append(text[cursor:])
    return "".join(output)


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _find_math_close(text: str, start: int, delimiter: str) -> int:
    cursor = start
    while cursor < len(text):
        found = text.find(delimiter, cursor)
        if found < 0:
            return -1
        if _is_escaped(text, found):
            cursor = found + len(delimiter)
            continue
        if delimiter == "$" and (
            (found > 0 and text[found - 1] == "$")
            or (found + 1 < len(text) and text[found + 1] == "$")
        ):
            cursor = found + 1
            continue
        return found
    return -1


def _convert_math(text: str) -> tuple[str, dict[str, str]]:
    math_tokens: dict[str, str] = {}
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "$" or _is_escaped(text, cursor):
            output.append(text[cursor])
            cursor += 1
            continue
        display = text.startswith("$$", cursor)
        delimiter = "$$" if display else "$"
        expression_start = cursor + len(delimiter)
        close = _find_math_close(text, expression_start, delimiter)
        if close < 0:
            raise WritingRenderError("invalid_math", "Invalid LaTeX expression")
        expression = text[expression_start:close]
        if not expression.strip():
            raise WritingRenderError("invalid_math", "Invalid LaTeX expression")
        try:
            mathml = convert_latex(expression.strip(), display="block" if display else "inline")
        except Exception as error:
            raise WritingRenderError("invalid_math", "Invalid LaTeX expression") from error
        token = f"{PROTECTED_PREFIX}MATH{len(math_tokens)}END"
        math_tokens[token] = mathml
        output.append(token)
        cursor = close + len(delimiter)
    return "".join(output), math_tokens


def _prepare_markdown(body: str) -> tuple[str, dict[str, str]]:
    if PROTECTED_PREFIX in body:
        raise WritingRenderError("invalid_markdown", "Markdown contains a reserved token")
    if BODY_H1_PATTERN.search(body):
        raise WritingRenderError("body_h1", "Article body must not contain an H1 heading")
    code_tokens: list[str] = []
    protected = _protect_fenced_code(body, code_tokens)
    protected = _protect_matches(protected, INLINE_CODE_PATTERN, code_tokens)
    protected, math_tokens = _convert_math(protected)
    for index, original in enumerate(code_tokens):
        protected = protected.replace(f"{PROTECTED_PREFIX}CODE{index}END", original)
    return protected, math_tokens


def _slugifier() -> Callable[[str, str], str]:
    counts: Counter[str] = Counter()

    def slugify(value: str, separator: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        ascii_value = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
        base = re.sub(r"[^a-z0-9]+", separator, ascii_value.lower()).strip(separator) or "section"
        counts[base] += 1
        return base if counts[base] == 1 else f"{base}{separator}{counts[base]}"

    return slugify


def _plain_heading_label(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _flatten_toc(tokens: list[dict[str, object]]) -> tuple[TocEntry, ...]:
    entries: list[TocEntry] = []

    def visit(items: list[dict[str, object]]) -> None:
        for item in items:
            level = item.get("level")
            if level in {2, 3}:
                entries.append(
                    TocEntry(
                        level=level,  # type: ignore[arg-type]
                        anchor=str(item["id"]),
                        label=_plain_heading_label(str(item["name"])),
                    )
                )
            children = item.get("children")
            if isinstance(children, list):
                visit(children)

    visit(tokens)
    return tuple(entries)


def _allowed_attribute(tag: str, name: str, value: str, heading_tokens: Mapping[str, str]) -> bool:
    if tag == "a":
        return name in {"href", "title", "rel", "target"}
    if tag == "img":
        return name in {"src", "alt", "title"}
    if tag == "code" and name == "class":
        return bool(CODE_CLASS_PATTERN.fullmatch(value))
    if tag in {"h2", "h3"} and name == "title":
        return value in heading_tokens
    return False


def _protect_heading_ids(
    author_html: str, toc: tuple[TocEntry, ...]
) -> tuple[str, dict[str, str]]:
    heading_tokens: dict[str, str] = {}
    protected_html = author_html
    for index, entry in enumerate(toc):
        token = f"{PROTECTED_PREFIX}HEADING{index}END"
        opening = f'<h{entry.level} id="{escape(entry.anchor, quote=True)}">'
        protected_opening = f'<h{entry.level} title="{token}">'
        if opening not in protected_html:
            raise WritingRenderError("invalid_markdown", "Unable to preserve article heading anchor")
        protected_html = protected_html.replace(opening, protected_opening, 1)
        heading_tokens[token] = entry.anchor
    return protected_html, heading_tokens


def _validate_asset(article: WritingArticle, value: str) -> tuple[Path, str]:
    split = urlsplit(value)
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or split.scheme
        or split.netloc
        or split.query
        or split.fragment
        or path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or path.as_posix() != value
        or len(path.parts) < 2
        or path.parts[0] != "assets"
        or ".." in path.parts
        or "." in path.parts
    ):
        raise WritingRenderError("invalid_asset", "Image path must stay below the article assets directory")
    bundle_root = article.bundle_root.resolve()
    source = (bundle_root / Path(*path.parts)).resolve()
    if not source.is_relative_to(bundle_root):
        raise WritingRenderError("invalid_asset", "Image path must stay below the article assets directory")
    if source.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
        raise WritingRenderError("unsupported_asset", "Image extension is not supported")
    if not source.is_file():
        raise WritingRenderError("missing_asset", "Image asset does not exist")
    under_assets = PurePosixPath(*path.parts[1:]).as_posix()
    return source, f"assets/{article.slug}/{under_assets}"


class _ArticleHtmlNormalizer(HTMLParser):
    def __init__(self, article: WritingArticle, heading_tokens: Mapping[str, str]) -> None:
        super().__init__(convert_charrefs=False)
        self.article = article
        self.heading_tokens = heading_tokens
        self.output: list[str] = []
        self.assets: list[AssetCopy] = []
        self.asset_destinations: set[str] = set()

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        normalized = [(name, value or "") for name, value in attrs]
        if tag in {"h2", "h3"}:
            values = dict(normalized)
            heading_token = values.pop("title", "")
            normalized = [(name, value) for name, value in normalized if name != "title"]
            if heading_token in self.heading_tokens:
                normalized.append(("id", self.heading_tokens[heading_token]))
        if tag == "a":
            values = dict(normalized)
            href = values.get("href", "")
            split = urlsplit(href)
            if split.scheme in {"http", "https"} or (not split.scheme and split.netloc):
                if split.netloc:
                    values["target"] = "_blank"
                    values["rel"] = "noopener noreferrer"
                else:
                    values.pop("href", None)
                    values.pop("target", None)
                    values.pop("rel", None)
            elif split.scheme not in {"", "mailto"}:
                values.pop("href", None)
                values.pop("target", None)
                values.pop("rel", None)
            else:
                values.pop("target", None)
                values.pop("rel", None)
            order = [name for name, _ in normalized if name not in {"target", "rel"}]
            if "rel" in values:
                order.extend(["rel", "target"])
            normalized = [(name, values[name]) for name in order if name in values]
        elif tag == "img":
            values = dict(normalized)
            source, destination = _validate_asset(self.article, values.get("src", ""))
            values["src"] = destination
            normalized = [(name, values[name]) for name, _ in normalized if name in values]
            if destination not in self.asset_destinations:
                self.asset_destinations.add(destination)
                self.assets.append(AssetCopy(source, destination))
        attributes = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in normalized)
        ending = " />" if self_closing else ">"
        self.output.append(f"<{tag}{attributes}{ending}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, True)

    def handle_endtag(self, tag: str) -> None:
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")


def render_article(
    article: WritingArticle, *, output_file: Path, output_root: Path
) -> RenderedArticle:
    """Render one article body into sanitized HTML plus copy/TOC instructions."""
    del output_file, output_root  # Reserved for the public renderer contract and future link policies.
    prepared, math_tokens = _prepare_markdown(article.body)
    renderer = markdown.Markdown(
        extensions=["extra", "sane_lists", TocExtension(slugify=_slugifier(), toc_depth="2-3")],
        output_format="html5",
    )
    renderer.treeprocessors.register(_RemoveHeadingIds(renderer), "force_heading_slugs", 6)
    author_html = renderer.convert(prepared)
    toc = _flatten_toc(renderer.toc_tokens)
    author_html, heading_tokens = _protect_heading_ids(author_html, toc)
    sanitized = bleach.clean(
        author_html,
        tags=ALLOWED_TAGS,
        attributes=lambda tag, name, value: _allowed_attribute(tag, name, value, heading_tokens),
        protocols={"http", "https", "mailto", "data"},
        strip=True,
        strip_comments=True,
    )
    normalizer = _ArticleHtmlNormalizer(article, heading_tokens)
    normalizer.feed(sanitized)
    normalizer.close()
    rendered_html = "".join(normalizer.output)
    for token, mathml in math_tokens.items():
        rendered_html = rendered_html.replace(token, mathml)
    return RenderedArticle(rendered_html, toc, tuple(normalizer.assets))


def _writings_relative_link(output_file: Path, output_root: Path, target: str) -> str:
    writings_root = Path(output_root).resolve() / "writings"
    source_parent = Path(output_file).resolve().parent
    target_path = writings_root / Path(*PurePosixPath(target).parts)
    return posixpath.relpath(target_path.as_posix(), source_parent.as_posix())


def _article_toc_navigation(toc: tuple[TocEntry, ...], back_href: str) -> str:
    lines = [f'      <a class="context-overview" href="{escape(back_href, quote=True)}">← 全部文章</a>']
    list_open = False
    item_open = False
    children_open = False
    for item in toc:
        link = f'<a href="#{escape(item.anchor, quote=True)}">{escape(item.label)}</a>'
        if item.level == 2:
            if children_open:
                lines.append("          </ul>")
                children_open = False
            if item_open:
                lines.append("        </li>")
            if not list_open:
                lines.append('      <ul class="writing-toc">')
                list_open = True
            lines.append(f"        <li>{link}")
            item_open = True
        elif item_open:
            if not children_open:
                lines.append('          <ul class="writing-toc-children">')
                children_open = True
            lines.append(f"            <li>{link}</li>")
        else:
            lines.append(f'      <a class="writing-toc-orphan" href="#{escape(item.anchor, quote=True)}">{escape(item.label)}</a>')
    if children_open:
        lines.append("          </ul>")
    if item_open:
        lines.append("        </li>")
    if list_open:
        lines.append("      </ul>")
    return "\n".join(lines)


def render_article_page(
    article: WritingArticle,
    rendered: RenderedArticle,
    *,
    output_file: Path,
    output_root: Path,
) -> str:
    """Wrap one rendered article in the shared site shell."""
    back_href = _writings_relative_link(output_file, output_root, "index.html")
    kind_href = _writings_relative_link(output_file, output_root, f"kind/{article.kind}.html")
    tag_links = " ".join(
        f'<a class="writing-tag" href="{escape(_writings_relative_link(output_file, output_root, f"tag/{tag}.html"), quote=True)}">#{escape(tag)}</a>'
        for tag in article.tags
    )
    main_content = f"""    <article class="writing-article">
      <header class="writing-header">
        <h1>{escape(article.title)}</h1>
        <p class="writing-meta"><time datetime="{article.published_at.isoformat()}">{article.published_at.isoformat()}</time> · <a href="{escape(kind_href, quote=True)}">{escape(KIND_LABELS[article.kind])}</a></p>
        <p class="writing-summary">{escape(article.summary)}</p>
        <p class="writing-tags">{tag_links}</p>
      </header>
      <div class="writing-body">{rendered.html}</div>
    </article>
"""
    return render_site_page(
        output_file=output_file,
        output_root=output_root,
        active_section="writings",
        page_title=f"{article.title} · TOGOS",
        meta_description=article.summary,
        secondary_navigation=_article_toc_navigation(rendered.toc, back_href),
        main_content=main_content,
        body_class="writing-article-page",
        head_content="  <style>.writing-body { max-width: 52rem; }</style>\n",
    )


def _filtered_records(
    records: Mapping[str, ManifestArticle], active_filter: tuple[str, str] | None
) -> list[tuple[str, ManifestArticle]]:
    if active_filter is None:
        selected = list(records.items())
    else:
        filter_type, value = active_filter
        if filter_type == "kind":
            selected = [(slug, record) for slug, record in records.items() if record.kind == value]
        elif filter_type == "tag":
            selected = [(slug, record) for slug, record in records.items() if value in record.tags]
        else:
            raise ValueError("active_filter must be a kind or tag filter")
    return sorted(
        selected,
        key=lambda item: (-date.fromisoformat(item[1].published_at).toordinal(), item[0]),
    )


def _index_navigation(
    records: Mapping[str, ManifestArticle],
    active_filter: tuple[str, str] | None,
    output_file: Path,
    output_root: Path,
) -> str:
    kind_counts = Counter(record.kind for record in records.values())
    tag_counts = Counter(tag for record in records.values() for tag in record.tags)

    def link(target: str, label: str, filter_value: tuple[str, str] | None) -> str:
        active = " is-active" if active_filter == filter_value else ""
        current = ' aria-current="page"' if active else ""
        href = _writings_relative_link(output_file, output_root, target)
        return f'      <a class="context-filter{active}" href="{escape(href, quote=True)}"{current}>{escape(label)}</a>'

    lines = [link("index.html", f"全部文章 ({len(records)})", None)]
    for kind in ("learning-note", "book-note"):
        lines.append(link(f"kind/{kind}.html", f"{KIND_LABELS[kind]} ({kind_counts[kind]})", ("kind", kind)))
    for tag in sorted(tag_counts):
        lines.append(link(f"tag/{tag}.html", f"{tag} ({tag_counts[tag]})", ("tag", tag)))
    return "\n".join(lines)


def render_writings_index(
    records: Mapping[str, ManifestArticle],
    *,
    active_filter: tuple[str, str] | None,
    output_file: Path,
    output_root: Path,
) -> str:
    """Render the chronological writings stream or one static filter view."""
    selected = _filtered_records(records, active_filter)
    if selected:
        rows = []
        for slug, record in selected:
            article_href = _writings_relative_link(output_file, output_root, record.page)
            kind_href = _writings_relative_link(output_file, output_root, f"kind/{record.kind}.html")
            tags = " ".join(
                f'<a href="{escape(_writings_relative_link(output_file, output_root, f"tag/{tag}.html"), quote=True)}">#{escape(tag)}</a>'
                for tag in record.tags
            )
            rows.append(f"""      <article class="writing-row">
        <p class="writing-row-meta"><time datetime="{escape(record.published_at, quote=True)}">{escape(record.published_at)}</time> · <a href="{escape(kind_href, quote=True)}">{escape(KIND_LABELS[record.kind])}</a></p>
        <h2><a href="{escape(article_href, quote=True)}">{escape(record.title)}</a></h2>
        <p>{escape(record.summary)}</p>
        <p class="writing-row-tags">{tags}</p>
      </article>""")
        stream = "\n".join(rows)
    else:
        stream = '      <p class="empty-section-copy">公开的学习笔记和读书笔记会在这里汇集。</p>'
    main_content = f"""    <section class="writings-stream" aria-labelledby="writings-title">
{render_section_intro("writings")}
      <h1 id="writings-title">文章</h1>
{stream}
    </section>
"""
    return render_site_page(
        output_file=output_file,
        output_root=output_root,
        active_section="writings",
        page_title="谈笑风生 · TOGOS",
        meta_description="还是要提高自己的知识水平",
        secondary_navigation=_index_navigation(records, active_filter, output_file, output_root),
        main_content=main_content,
        body_class="writings-index-page",
    )
