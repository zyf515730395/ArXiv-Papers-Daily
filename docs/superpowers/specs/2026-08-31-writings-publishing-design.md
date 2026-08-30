# Writings Publishing V2 Design

## Summary

TOGOS V2 turns `谈笑风生` from an empty section into a deterministic static article publisher. Public Markdown bundles in the repository are the only source of truth. The publisher validates metadata, renders technical Markdown and LaTeX to self-contained HTML, produces chronological and filtered article indexes, and adds public article titles to the existing site-wide search index.

The publisher is deliberately independent from future ingestion. Notion and WeChat Reading adapters will eventually create the same repository format, but they do not participate in V2 publication. A malformed article must not prevent other articles from publishing: new failures are skipped, while previously published failures retain their last known-good output.

## Goals

- Publish repository-owned learning notes and book notes under `docs/writings/`.
- Preserve the approved `书脊目录` shell, typography, light/dark themes, global search, responsive drawer, and secondary-sidebar behavior.
- Support technical Markdown: headings, lists, blockquotes, tables, fenced code, links, local images, inline LaTeX, and block LaTeX.
- Render complete article HTML at build time; do not require client-side Markdown or formula rendering.
- Make article URLs stable and independent of later title edits.
- Generate chronological, type-filtered, and tag-filtered static indexes without client-side filtering.
- Continue publishing valid articles when another article is malformed.
- Keep the previous successful version of an already-published article when its next build fails.
- Produce actionable, non-sensitive local and CI diagnostics for failed articles.
- Preserve the existing paper archive, milestone pages, 8,759-record baseline search behavior, and GitHub Pages deployment.

## Non-goals

- No Notion API access, Notion credentials, or Notion-to-Markdown conversion.
- No WeChat Reading parser or WSL inference request.
- No online editor, authentication, comments, drafts, private articles, recommendations, analytics, or full-text search.
- No remote article images, embedded third-party media, or raw SVG assets.
- No fabricated example article committed to the repository.
- No README changes.

## Chosen architecture

V2 adds a focused Python publishing domain beside `papers` and `milestones`. It reuses the shared site shell, search-document contract, Markdown sanitizer, atomic output helpers, and GitHub Pages layout. It does not introduce a second static-site generator or a frontend framework.

The alternatives were rejected for these reasons:

- A separate generator such as MkDocs would create competing templates, route rules, navigation ownership, and deployment commands.
- Browser-side Markdown rendering would weaken first paint, offline reliability, indexing, security review, and graceful no-JavaScript behavior.
- Notion-first publication would make network access and credentials part of the public deployment path.

## Source layout and ownership

Every public article is a self-contained bundle:

```text
content/writings/<slug>/
├── index.md
└── assets/
    ├── diagram.png
    └── result.webp
```

`content/writings/` contains published material only. A file with `public: false` is not private because the repository itself exposes its source; V2 therefore treats any value other than the literal boolean `true` as an article error. Private drafts stay outside the repository and are not part of this design.

The directory name is the canonical article identity. Removing the whole bundle is the only V2 unpublish operation. Changing the title, summary, tags, or body does not change the article URL.

## Front matter contract

Every `index.md` begins with YAML front matter:

```yaml
---
title: 文章标题
slug: stable-slug
published_at: 2026-08-31
kind: learning-note
public: true
summary: 一句话摘要
tags:
  - diffusion
source: original
---
```

Fields are strict:

| Field | Contract |
| --- | --- |
| `title` | Required non-empty string after trimming. |
| `slug` | Required lowercase ASCII kebab-case string and exactly equal to the bundle directory name. |
| `published_at` | Required ISO `YYYY-MM-DD` calendar date. |
| `kind` | Required; exactly `learning-note` or `book-note`. |
| `public` | Required; exactly YAML boolean `true`. |
| `summary` | Required non-empty plain-text string; line breaks and HTML are rejected. |
| `tags` | Required non-empty list of unique lowercase kebab-case strings; order is preserved for display. |
| `source` | Required; exactly `original`, `notion`, or `wechat-reading`. It records public provenance only. |

External page IDs, API credentials, local absolute paths, import timestamps, and private mapping metadata are forbidden. Future importers keep those values in a separate ignored local state store.

Unknown front matter fields are rejected so misspellings cannot silently alter publication behavior. Tags are case-sensitive at validation time and must already be normalized rather than silently rewritten.

## Domain model and module boundaries

The implementation introduces these units:

| File | Responsibility |
| --- | --- |
| `src/writings/models.py` | Immutable `WritingArticle`, `WritingIssue`, `WritingManifest`, and `WritingBuildResult` contracts. |
| `src/writings/catalog.py` | Discover bundles, parse YAML, validate fields and local asset references, and associate a failed bundle with its prior manifest record. |
| `src/writings/rendering.py` | Render sanitized technical Markdown, stable H2/H3 anchors, article TOC, local images, code blocks, tables, and static MathML. |
| `src/writings/publisher.py` | Prepare the complete desired output tree, chronological/filter pages, article pages, assets, manifest, reports, and `SearchDocument` objects. |
| `src/papers/site.py` | Invoke the writings publisher from the existing unified site generation flow and merge article search documents before writing the public search index. |
| `src/shared/site_shell.py` | Reuse the current shell; only add the article-specific head/body extension points required by writings pages. |
| `src/shared/rendering.py` | Retain shared safe HTML and atomic helpers; reusable allow-list behavior may move here when it serves more than writings. |
| `docs/assets/css/site.css` | Add editorial index, readable article body, TOC, table, code, image, and MathML styles. |
| `.github/workflows/togos-daily.yml` | Stage managed writings output and surface per-article warnings without treating degraded publication as a fatal build. |
| `.gitignore` | Ignore `/build/` before the publisher writes local reports there. |

The writings publisher returns structured data rather than directly mutating the global search payload. Papers, milestones, and writings remain owners of their domain records; `write_search_index` remains the single serializer.

## Technical Markdown rendering

The body pipeline is deterministic:

1. Parse and remove YAML front matter.
2. Protect fenced code and inline code before scanning math delimiters.
3. Recognize `$...$` for inline math and `$$...$$` for block math; escaped delimiters remain literal text.
4. Convert LaTeX with `latex2mathml>=3.81,<4` through `latex2mathml.converter.convert(latex, display=...)`.
5. Render Markdown with the existing Python-Markdown stack plus `extra`, `sane_lists`, `toc`, and deterministic heading slugification.
6. Sanitize author-controlled HTML with Bleach. Raw HTML not present in the allow-list is stripped.
7. Restore trusted converter-produced MathML into protected positions after author HTML sanitization.
8. Rewrite and validate local image URLs before copying assets.

`latex2mathml` is a pure-Python converter whose maintained API accepts LaTeX and an inline/block display mode, allowing formula output without runtime JavaScript. Its version range is added to `pyproject.toml`.

Supported image extensions are `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, and `.avif`. Image paths must be relative, remain under the current bundle's `assets/` directory after resolution, and point to an existing regular file. Absolute URLs, protocol-relative URLs, data URLs, root-relative paths, backslashes, symlinks escaping the bundle, and `..` traversal are rejected.

Article links may target safe `http`, `https`, `mailto`, fragment, or site-relative URLs. External links receive `rel="noopener noreferrer"`. Code language classes are retained only when they match `language-[a-z0-9_+-]+`.

Only H2 and H3 headings enter the article TOC. Heading IDs use Unicode NFKC normalization, lowercase ASCII slugging when possible, and stable numeric suffixes for duplicates. An article has one page-level H1 generated from front matter; an H1 in the Markdown body is rejected.

## Routes and information architecture

Generated routes are:

```text
/writings/index.html
/writings/<slug>.html
/writings/kind/learning-note.html
/writings/kind/book-note.html
/writings/tag/<tag>.html
/writings/assets/<slug>/...
/writings/manifest.json
```

The main writings index is an editorial chronological stream sorted by `published_at` descending and then `slug` ascending. Each entry shows date, localized kind, title, summary, and tags. It does not use a card grid.

The index secondary sidebar links to all articles, both kind pages, and tag pages with counts. Filter pages use the same chronological entry renderer and remain fully usable without JavaScript. With no successfully published or retained articles, the main index keeps the approved empty-state copy.

Article detail pages use the shared `谈笑风生` primary selection and section intro. The context sidebar contains a back link to the writings index and the current article's H2/H3 TOC; it never repeats the section name or section description. The article header shows title, date, localized kind, and tags. The body uses a reading measure of approximately `52rem`; code blocks and wide tables scroll inside their own containers and never widen the page.

Every successful or retained article creates exactly one search document:

```text
id: article:<slug>
url: writings/<slug>.html
section: writings
kind: article
published_at: <front matter date>
```

Article titles join the existing title-only global search. Body text, tags, summaries, source metadata, and failed new articles do not enter `search-index.json`.

## Managed manifest and stale output

`docs/writings/manifest.json` is the public, versioned record of generated writings output. It contains only public metadata and repository-relative managed paths:

```json
{
  "version": 1,
  "generated_at": "2026-08-31",
  "articles": {
    "stable-slug": {
      "source": "content/writings/stable-slug/index.md",
      "title": "文章标题",
      "published_at": "2026-08-31",
      "kind": "learning-note",
      "summary": "一句话摘要",
      "tags": ["diffusion"],
      "page": "stable-slug.html",
      "assets": ["assets/stable-slug/diagram.png"]
    }
  },
  "managed_files": ["stable-slug.html", "assets/stable-slug/diagram.png"]
}
```

Every manifest path is validated as a normalized relative POSIX path under `docs/writings/`. An invalid, duplicate, absolute, or traversing path is a global fatal error. Cleanup computes `previous managed_files - next managed_files` and may remove only that validated difference.

The complete desired `docs/writings/` tree is prepared in a sibling temporary directory. Retained last known-good files are copied only when listed by the validated prior manifest. Promotion uses a backup-and-rename transaction inside the resolved output root; if promotion fails, the previous directory is restored. Temporary and backup paths are removed only after their resolved locations are confirmed inside the configured output root.

## Per-article fault tolerance

Each source bundle is isolated through discovery, metadata validation, Markdown rendering, math conversion, and asset validation.

- A successful article replaces its previous manifest record and generated files.
- A newly discovered article that fails is skipped from all pages, filters, manifest records, and search documents.
- A previously published article that fails retains its prior manifest record and all last known-good managed files byte-for-byte.
- A source bundle removed from `content/writings/` is an explicit unpublish and is absent from the next manifest, pages, filters, and search index.
- `public: false` is an article error, not an unpublish signal; the previous version remains online when one exists.

Valid and retained articles are used to regenerate the main index, kind pages, tag pages, manifest, and article search documents. A degraded build exits successfully after emitting warnings so unrelated valid updates can reach the site.

The publisher returns a `WritingBuildResult` containing successful, retained, skipped, removed, search-document, and issue collections. It writes a local report to `build/reports/writings.json` with this shape:

```json
{
  "version": 1,
  "generated_at": "2026-08-31",
  "status": "degraded",
  "counts": {"published": 3, "retained": 1, "skipped": 1, "removed": 0},
  "issues": [
    {
      "source": "content/writings/broken/index.md",
      "code": "missing_asset",
      "message": "Referenced asset does not exist: assets/plot.png"
    }
  ]
}
```

Reports never contain article bodies, absolute paths, credentials, external source IDs, environment values, or stack traces. CI prints one warning per issue and a final count summary. The report remains ignored and is not copied into `docs/`.

## Global fatal errors

These conditions still abort publication and return a non-zero exit status:

- The existing writings manifest is malformed, unsupported, or contains an unsafe managed path.
- The configured source, output, staging, backup, or report root resolves outside its allowed boundary.
- The output directory cannot be staged, promoted, restored, or written safely.
- The Markdown or MathML renderer cannot initialize.
- The public index or search index cannot be serialized or committed consistently.
- Duplicate output routes remain after per-article isolation.
- A failure prevents preservation of an article that must retain its last known-good output.

Global validation occurs before promotion. When a global error occurs during promotion, rollback restores the prior writings directory and the prior search index.

## Build integration

The existing unified site generation command remains the public entry point. It loads the prior writings manifest, prepares the writings publication plan, combines its successful/retained article search records with papers and milestones, and then commits writings output and the search index with rollback support.

The daily GitHub workflow stages `docs/writings/` and `docs/search-index.json` along with current archive outputs. Per-article warnings do not stop the workflow. Global fatal errors stop before commit and deployment. The workflow never stages `build/` or test artifacts.

## Testing strategy

All new tests, fixtures, temporary reports, snapshots, and rendered test sites remain local-only under ignored test/temp directories and are deleted before each feature commit, as required by `AGENTS.md`.

The test matrix includes:

- Strict validation for every front matter field, unknown fields, normalization, slug-directory equality, and H1 rejection.
- Safe and unsafe image paths, missing images, permitted extensions, escaping symlinks, remote URLs, and asset copying.
- Fenced/inline code protection, tables, links, sanitization, stable H2/H3 anchors, duplicate headings, inline math, block math, escaped delimiters, and invalid LaTeX.
- Chronological index order, localized kind labels, tag counts, static filter routes, article detail TOC, and zero-article empty state.
- One valid and one invalid new article: valid output publishes, invalid output is skipped and recorded, and the build status is degraded.
- A previously published article made invalid: its page/assets/metadata/search record remain byte-for-byte last known-good while other changes publish.
- A removed source bundle: only prior manifest-managed files are removed and the search record disappears.
- A malformed or unsafe prior manifest: publication is globally fatal and existing `docs/writings/` plus `search-index.json` remain unchanged.
- Report safety: only repository-relative paths and safe messages are emitted; bodies, absolute paths, source IDs, and secrets are absent.
- Search integration preserves all current paper/model records and adds one title result per published or retained article.
- Browser verification at 390, 768, and 1440 pixels covers index, filter, detail, TOC, table, code, image, MathML, dark theme, drawer, and sidebar collapse behavior with no horizontal page overflow.

Implementation commits are split by independently reviewable behavior:

1. Article contracts, validation, reports, and manifest safety.
2. Technical Markdown, static MathML, local assets, and article-page rendering.
3. Chronological/filter pages, search integration, transactional publication, workflow changes, and generated outputs.

Before every commit, local tests are run, test artifacts are deleted, staged files are inspected, `git diff --cached --check` passes, and `README.md` remains unchanged.

## Acceptance criteria

V2 is complete when:

- A valid repository article generates all expected static pages, local assets, TOC, static formulas, manifest data, and one title-search record.
- An invalid new article is skipped and reported without blocking valid articles.
- An invalid update to an existing article preserves the prior public version and search record.
- Removing an article bundle unpublishes only its manifest-managed output.
- All four knowledge sections, existing paper/model functionality, 8,759 baseline search records, responsive layout, light/dark themes, navigation collapse, and keyboard behavior remain operational.
- No private metadata, local report, temporary file, test artifact, remote image, fake article, or README change is committed.

## Dependency references

- `latex2mathml` package and current release metadata: <https://pypi.org/project/latex2mathml/>
- Maintained converter API: <https://github.com/roniemartinez/latex2mathml>
