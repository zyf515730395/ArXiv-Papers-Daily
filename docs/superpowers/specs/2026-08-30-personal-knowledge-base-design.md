# Personal Knowledge Base V1 Design

## Summary

TOGOS will evolve from a two-section paper archive into a public personal knowledge base without replacing its Python 3.12 static-generation workflow or GitHub Pages deployment. V1 preserves the arXiv collector, archive structure, paper summaries, milestone catalog, model timelines, and comparison pages while adding a shared four-section shell, site-wide public-title search, a persistent secondary-sidebar collapse control, and finished empty states for future writing and travel content.

The visual direction is the approved “Knowledge Compass” minimal design: typography, whitespace, hairline rules, and one compass-blue position marker establish hierarchy. The site must not use gradients, decorative card stacks, or ornamental motion.

## Goals and non-goals

### Goals

- Rename the current primary sections to `学习一个` and `身经百战` without changing their content behavior.
- Use the exact secondary descriptions:
  - `学习一个`: `毕竟还too young，感觉还要学习一个`
  - `身经百战`: `这些模型是身经百战了`
- Add `谈笑风生` with the exact description `还是要提高自己的知识水平`.
- Add `跑得还快` with the description `记录走过的城市与拍下的瞬间`.
- Make all four sections reachable through the shared primary navigation.
- Search titles across every public content type through one site-wide interface.
- Let desktop users collapse and restore the secondary sidebar without losing their choice between pages.
- Preserve light and dark themes, responsive behavior, keyboard access, and GitHub Pages deployment.

### V1 non-goals

- No live Notion API integration, credentials, or private-page synchronization.
- No WeChat Reading export parser or book-note summarization pipeline.
- No browser connection to the WSL inference service.
- No map SDK, real city data, or example travel photos.
- No authentication, private index, database, server runtime, or frontend-framework migration.
- No changes to `README.md`.

## Information architecture and routes

The primary navigation is a single ordered registry used by every generated page:

| Key | Label | Description | Canonical route | V1 state |
| --- | --- | --- | --- | --- |
| `learning` | 学习一个 | 毕竟还too young，感觉还要学习一个 | `/index.html` | Existing archive, restyled |
| `milestones` | 身经百战 | 这些模型是身经百战了 | `/milestone-models/flux.html` | Existing model pages, restyled |
| `writings` | 谈笑风生 | 还是要提高自己的知识水平 | `/writings/index.html` | Finished empty state |
| `journeys` | 跑得还快 | 记录走过的城市与拍下的瞬间 | `/journeys/index.html` | Finished empty state |

The existing `/notes/` directory remains reserved for generated paper-summary pages. The new writing section uses `/writings/` to avoid changing or overloading that contract. All pages use relative links calculated from their output depth so the site works both at the GitHub Pages project path and from a local HTTP server. The shared shell writes that calculated prefix to `body[data-site-root]`; global JavaScript resolves the search index and cross-section links from this value rather than assuming a domain-root deployment.

The primary section names, archive topic names, milestone topic names, model-family names, dates, and paper metadata are separate concepts. Renaming primary sections must not rewrite archive topics such as `Image Generation` or milestone catalog entries.

## Shared shell and visual system

Every full site page uses the same shell renderer and assets:

1. A fixed primary sidebar containing the TOGOS brand and four sections.
2. A context-sensitive secondary sidebar containing the active section title, required description, and section navigation.
3. A page workspace with the global search trigger, theme control, page heading, and content.

Every full page declares `lang="zh-CN"` because navigation, controls, and primary explanatory copy are Chinese even when paper titles are English.

Desktop layout tokens:

- Primary sidebar: `9rem`.
- Expanded secondary sidebar: `12rem`.
- Collapsed secondary rail: `2.75rem`.
- Content width consumes the remaining viewport and must never inherit the current hard-coded combined navigation margin.

At widths up to `900px`, the navigation becomes one off-canvas drawer. The desktop collapsed preference does not hide context inside the mobile drawer. At widths up to `560px`, paper tables become readable stacked rows rather than cropped desktop tables; identifiers, title, authors, and summary state remain available.

Light palette:

- Canvas `#FBFCFD`
- Surface `#FFFFFF`
- Ink `#111418`
- Muted `#66707B`
- Rule `#E3E7EB`
- Compass blue `#2258E6`

Dark palette:

- Canvas `#0D1117`
- Surface `#11161D`
- Ink `#F2F4F7`
- Muted `#98A2AE`
- Rule `#28303A`
- Compass blue `#7EA2FF`

Typography uses platform-native Chinese sans-serif faces (`PingFang SC`, `Microsoft YaHei`, `Noto Sans SC`) with the system sans-serif fallback. Dates, counts, IDs, and keyboard hints use the platform monospace stack. V1 makes no external font requests.

The compass-blue marker is the only persistent accent. Motion is limited to the secondary-sidebar width transition and search-dialog entrance, both at no more than `160ms`; `prefers-reduced-motion` disables them.

## Secondary-sidebar behavior

The secondary sidebar header contains one button with a visible directional glyph, an accessible name, `aria-expanded`, and `aria-controls` targeting the contextual navigation.

- Desktop default is expanded.
- Collapsing adds one root state attribute before first paint and reduces the secondary sidebar to the `2.75rem` compass rail.
- The preference is stored under `togos-secondary-sidebar-collapsed` and shared by all pages.
- If storage is unavailable or contains an invalid value, the site remains expanded and fully usable.
- Expanding restores the same scroll position and context; it does not navigate or reload.
- On mobile, the drawer always exposes both navigation levels regardless of the saved desktop state.
- `Escape` closes the mobile drawer and the search dialog but does not change the saved desktop preference.
- With JavaScript disabled, both sidebars render expanded and every navigation link works.

## Public content and search index

### Generated interface

The build emits `/search-index.json` with this stable versioned shape:

```json
{
  "version": 1,
  "generated_at": "2026-08-30",
  "documents": [
    {
      "id": "paper:2608.26993",
      "title": "A public paper title",
      "url": "index.html#paper-2608.26993",
      "section": "learning",
      "kind": "paper",
      "published_at": "2026-08-30"
    }
  ]
}
```

Each search document contains exactly `id`, `title`, `url`, `section`, `kind`, and optional `published_at`. Supported V1 kinds are `paper`, `model`, and `article`. Search results display localized section and kind labels from the shared registry rather than duplicating user-facing strings in the JSON.

All current archive papers and ready milestone pages are public. Future imported articles enter the index only when their normalized source record explicitly contains `public: true`. Private records are discarded before page and index generation; private titles, Notion IDs, source URLs, credentials, and local paths never appear in `docs/`.

Paper IDs are canonical. A paper with a ready local summary produces one search result whose URL opens the summary anchor. A paper without a ready summary points to its stable archive row anchor. The same paper must not appear twice because it is present in both the archive and a summary manifest.

### Client behavior

- `Ctrl+K` and `Cmd+K` open search from every full site page.
- The visible search trigger is labeled `搜索公开文章标题`.
- The client fetches the index only when search is opened for the first time, then caches it for the page lifetime.
- Nested pages resolve the index by concatenating `body.dataset.siteRoot` with `search-index.json`; they never fetch `/search-index.json` as an absolute domain-root path.
- Query normalization uses Unicode NFKC, lowercase conversion, and whitespace tokenization.
- Every query token must be a substring of the normalized title.
- Ranking order is exact title, title prefix, earliest token position, then newest `published_at`.
- At most 20 results render at once.
- Arrow keys move the active result, `Enter` opens it, and `Escape` closes search and returns focus to the trigger.
- Empty query shows a short instruction; no match shows `没有找到公开标题`.
- A failed or invalid index shows `搜索索引加载失败，请刷新后重试`; the rest of the page remains operational.

## Section-specific pages

### 学习一个

The archive keeps its current topic, year, month, and week hierarchy, summary dialog, candidate states, theme control, and deep-link behavior. The main heading becomes `今天学什么`. Desktop rows retain dense scanning; mobile rows reflow into labeled blocks without horizontal page overflow.

### 身经百战

Milestone family navigation, status semantics, timeline dragging, release sources, comparison tables, and article-reading links remain unchanged. User-facing occurrences of the primary section and secondary heading use `身经百战`; model and catalog terminology does not change.

### 谈笑风生

The empty page uses the shared shell, the exact section description, and this body copy: `公开的学习笔记和读书笔记会在这里汇集。`. It does not display fake cards, article counts, unavailable filters, or a disabled import control.

Future normalized article files will use Markdown with front matter fields `title`, `slug`, `published_at`, `kind`, `source`, `public`, and `tags`. `kind` is either `learning-note` or `book-note`; `source` records provenance without exposing credentials.

### 跑得还快

The empty page uses the shared shell and this body copy: `城市坐标与摄影作品会在这里出现。`. It does not download map libraries, request location permission, show fake pins, or embed placeholder photography.

A later map pipeline will consume a separate public city manifest; V1 does not define or ship that manifest because no real city or photo input is yet in scope.

## Build flow and module boundaries

The existing collectors and milestone catalog remain domain owners. The refactor introduces three shared responsibilities:

1. **Section registry and paths** — owns section labels, descriptions, canonical routes, active state, and depth-aware links.
2. **Shell rendering** — owns common head bootstrapping, primary/secondary navigation, controls, empty-state layout, and shared asset references.
3. **Search publishing** — accepts public documents from paper, milestone, and future article adapters; validates IDs and required fields; de-duplicates by ID; writes the versioned index atomically.

Paper and milestone modules continue to own their domain markup. They call the shared shell and expose search documents rather than duplicating global navigation or search HTML.

The daily collection workflow generates the archive and search index in the same run. Milestone publishing also refreshes the same deterministic index input before final site generation. Empty section pages are generated from the section registry, not maintained as hand-written `docs/` files.

Generated output stays deterministic for a fixed input and date. All writes use the existing atomic rendering helper where practical so a failed generation cannot leave a partial index or page.

## Failure handling and accessibility

- Invalid source catalogs fail the build with a file-specific schema error; generators do not silently omit malformed public content.
- Duplicate search IDs fail the build unless they are the intentional paper/summary pair resolved by the canonical-paper rule.
- Missing optional summaries preserve the existing pending or empty summary state.
- Missing future writing or journey data generates the approved empty state rather than failing deployment.
- Search and sidebar controls have visible focus states and minimum `44px` touch targets on mobile.
- Search uses a labeled native `<dialog>`, matching the existing summary-dialog browser baseline.
- Color is never the only active-state signal; the blue compass marker is paired with weight and `aria-current`.
- Light and dark palettes meet WCAG AA contrast for body text and controls.

## Validation and acceptance

V1 is accepted only when all of the following are demonstrated:

1. Existing paper JSON parses to the same paper counts and chronological grouping before and after the refactor.
2. Existing ready milestone families, releases, timeline order, comparison values, sources, and notes remain present.
3. Every generated full page contains the four primary links, correct active state, and exact required descriptions.
4. `/writings/index.html` and `/journeys/index.html` render the approved empty states without fake content or external network requests.
5. The generated search index validates against version 1, contains unique IDs, excludes non-public fixtures, and resolves every URL to a generated file and optional anchor.
6. Search matching covers Chinese substrings, English case folding, multi-token queries, ranking, empty results, load failure, and keyboard navigation.
7. Sidebar collapse persists across full-page navigation, falls back safely when storage throws, and does not hide mobile context.
8. Deep links, month tabs, year expansion, summary dialog, theme selection, timeline drag-scroll, and mobile drawer continue to work.
9. Generated pages have no horizontal page overflow at `390px`, `768px`, `1280px`, and `1440px`; dense internal comparison scrollers may scroll within their own labeled viewport.
10. A local HTTP visual QA confirms the approved minimal desktop and mobile design in both themes, including reduced motion and keyboard focus.
11. All temporary tests, fixtures, screenshots, and coverage outputs created for the feature are removed before commits.
12. Commits are split by independent feature, use concise English messages, and pass `git diff --cached --check`; nothing is pushed.

## Rollout and future extension

The static site is regenerated locally before commits so source and `docs/` artifacts remain aligned. GitHub Pages deployment continues to publish only `docs/`; no workflow needs new secrets for V1.

Later Notion and WeChat Reading work will be separate import adapters that produce the normalized public article format. The WSL model is a build-time summarizer behind those adapters, never a browser dependency. The travel map will be a separate phase activated only after real public city coordinates and photographs are provided.
