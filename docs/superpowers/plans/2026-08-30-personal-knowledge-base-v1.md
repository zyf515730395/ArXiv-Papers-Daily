# Personal Knowledge Base V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing public paper tracker into the approved four-section, minimal “Knowledge Compass” personal knowledge base while preserving paper and milestone behavior.

**Architecture:** Keep Python 3.12 as the build-time owner and GitHub Pages as the only runtime. Add one shared section/shell module and one search-index publisher; paper and milestone modules retain domain markup, while small framework-free JavaScript modules own site-shell state and title search.

**Tech Stack:** Python 3.12, standard-library dataclasses/JSON/pathlib, existing PyYAML/Markdown/Bleach dependencies, semantic HTML, CSS custom properties, browser-native `<dialog>`, vanilla JavaScript, GitHub Actions, GitHub Pages.

**Spec:** `docs/superpowers/specs/2026-08-30-personal-knowledge-base-design.md`

## Global constraints

- Before the first implementation edit, run `git status --short --branch` and `git pull --ff-only`. Stop if the worktree is dirty or the pull fails; never reset or overwrite user work.
- Do not modify `README.md`.
- Feature tests are local-only. Put temporary tests under ignored `tests/`, never stage them, and delete them before the feature is considered complete.
- Use `shared.rendering.atomic_write_text` for generated HTML and JSON.
- Generated `docs/` artifacts belong in the same feature commit as the source that owns them.
- Before every commit, run `git status --short`, inspect `git diff --cached --stat`, and run `git diff --cached --check`.
- Use concise English commit messages and do not push.
- No Notion credentials, WeChat Reading parser, WSL browser call, map SDK, fake city data, external photography, frontend framework, or external font request in V1.

## Target file map

| File | Responsibility |
| --- | --- |
| `src/shared/site_shell.py` | Ordered section registry, depth-aware links, pre-paint state bootstrap, shared page shell, context sidebar, and empty-section rendering. |
| `src/shared/search_index.py` | Search document contract, validation, duplicate rejection, deterministic payload construction, and atomic JSON output. |
| `src/papers/site.py` | Paper archive/domain navigation, stable paper anchors, public paper search documents, and orchestration of all V1 outputs. |
| `src/milestones/catalog.py` | Milestone catalog validation and milestone-only secondary navigation; no primary navigation duplication. |
| `src/milestones/publisher.py` | Milestone page domain content using the shared shell and model search-document production. |
| `src/papers/collector.py` | Pass configured output root/date into the unified site build. |
| `config/site.yaml` | Explicit generated output root and search-index path. |
| `.github/workflows/togos-daily.yml` | Stage the generated search index alongside the daily archive outputs. |
| `docs/assets/js/site-shell.js` | Theme, mobile drawer, persistent desktop secondary-sidebar collapse, and safe storage handling. |
| `docs/assets/js/search-core.js` | DOM-free NFKC normalization, all-token matching, ranking, and 20-result limit. |
| `docs/assets/js/search.js` | Lazy index loading, dialog rendering, keyboard navigation, URL resolution, and focus restoration. |
| `docs/assets/js/sidebar.js` | Existing archive tabs/deep links, summary dialog, bulk details actions, and timeline drag-scroll only. |
| `docs/assets/css/site.css` | Approved minimal palette/layout, responsive paper rows, focus styles, dialog, dark theme, and reduced motion. |
| `docs/index.html` | Regenerated `学习一个` archive. |
| `docs/milestone-models/*.html` | Regenerated `身经百战` pages. |
| `docs/writings/index.html` | Generated `谈笑风生` empty state. |
| `docs/journeys/index.html` | Generated `跑得还快` empty state. |
| `docs/search-index.json` | Versioned public-title index. |

---

### Task 1: Establish the shared four-section shell

**Files:**

- Create: `src/shared/site_shell.py`
- Modify: `src/papers/site.py`
- Modify: `src/milestones/catalog.py`
- Modify: `src/milestones/publisher.py`
- Create locally, then delete: `tests/test_site_shell.py`

The registry is the only source for the four labels, descriptions, order, and canonical routes:

```python
@dataclass(frozen=True, slots=True)
class Section:
    key: str
    label: str
    description: str
    route: str


SECTIONS = (
    Section("learning", "学习一个", "毕竟还too young，感觉还要学习一个", "index.html"),
    Section("milestones", "身经百战", "这些模型是身经百战了", "milestone-models/flux.html"),
    Section("writings", "谈笑风生", "还是要提高自己的知识水平", "writings/index.html"),
    Section("journeys", "跑得还快", "记录走过的城市与拍下的瞬间", "journeys/index.html"),
)
```

The shared renderer exposes one explicit boundary instead of letting domain modules assemble outer HTML:

```python
def render_site_page(
    *,
    output_file: Path,
    output_root: Path,
    active_section: str,
    page_title: str,
    meta_description: str,
    secondary_navigation: str,
    main_content: str,
    body_class: str = "",
    trailing_dialogs: str = "",
) -> str: ...
```

`render_site_page` computes `site_root` from `output_file.relative_to(output_root).parent`, writes it to `body[data-site-root]`, renders all four primary links through that prefix, emits `lang="zh-CN"`, includes `site.css`, `site-shell.js`, `search-core.js`, `search.js`, and `sidebar.js`, and includes the shared search dialog/controls. The no-JavaScript markup remains fully expanded.

- [ ] Add `tests/test_site_shell.py` with assertions for section order, exact copy, nested `../` root calculation, four depth-aware links, one `aria-current="page"`, `lang="zh-CN"`, and `data-site-root`.
- [ ] Run `python -m unittest tests.test_site_shell -v`; confirm it fails because `shared.site_shell` does not exist.
- [ ] Implement `Section`, `SECTIONS`, `get_section(key)`, `site_root_for(output_file, output_root)`, `render_primary_navigation(...)`, `render_context_sidebar(...)`, and `render_site_page(...)`.
- [ ] Put the theme and collapse bootstrap inline in `<head>` so valid saved state is applied before first paint. Invalid/unavailable storage must produce the expanded default.
- [ ] Change `papers.site.render_sidebar` to return only archive context navigation and pass it to `render_site_page(active_section="learning", ...)`.
- [ ] Change `milestones.catalog.render_milestone_navigation` to return only model-family context navigation; remove `render_primary_sidebar` from this module.
- [ ] Change `milestones.publisher.render_family_page` to call `render_site_page(active_section="milestones", ...)` without touching timeline/comparison generation.
- [ ] Preserve focused paper-summary and milestone-notes reader pages as direct anchor targets; update their shared palette in Task 6, but do not fabricate a second navigation hierarchy inside article content.
- [ ] Run `python -m unittest tests.test_site_shell -v`; confirm all shell tests pass.
- [ ] Run `python -m compileall -q src`.
- [ ] Delete `tests/test_site_shell.py` and confirm `git status --short` lists no test file.
- [ ] Do not commit yet; Task 2 completes the first independently useful shell feature.

### Task 2: Generate the two future-section empty states and migrate current pages

**Files:**

- Modify: `src/shared/site_shell.py`
- Modify: `src/papers/site.py`
- Modify: `src/papers/collector.py`
- Modify: `config/site.yaml`
- Create: `docs/writings/index.html`
- Create: `docs/journeys/index.html`
- Modify: `docs/index.html`
- Modify: `docs/milestone-models/*.html`
- Create locally, then delete: `tests/test_site_generation.py`

Add one generated empty-page API and one orchestration entry point:

```python
def render_empty_section_page(
    *, output_file: Path, output_root: Path, section_key: str, body_copy: str
) -> str: ...


def generate_site(
    json_path: str | Path,
    output_path: str | Path,
    candidate_path: str | Path | None = None,
    milestone_catalog_path: str | Path = DEFAULT_MILESTONE_CATALOG,
    *,
    output_root: str | Path | None = None,
    generated_on: datetime.date | None = None,
) -> None: ...
```

`generated_on` makes tests deterministic; production defaults to `date.today()`. `output_root` defaults to `Path(output_path).parent` for compatibility.

- [ ] Add a temporary generation test that copies the smallest valid archive/catalog fixtures into `tests/tmp-site/`, records paper/category/release counts, calls `generate_site(..., generated_on=date(2026, 8, 30))`, and asserts those counts remain unchanged.
- [ ] Assert generated `index.html`, a ready milestone family page, `writings/index.html`, and `journeys/index.html` each contain four primary links and the correct active state.
- [ ] Assert exact copy: `今天学什么`, the three required section descriptions, `公开的学习笔记和读书笔记会在这里汇集。`, and `城市坐标与摄影作品会在这里出现。`.
- [ ] Assert the two empty pages contain no map script, location permission request, fake pin, fake article card, Notion token, or external image/font URL.
- [ ] Run the temporary test and confirm its empty-page assertions fail.
- [ ] Add `output_root: './docs'` and `search_index_path: './docs/search-index.json'` to `config/site.yaml`; keep existing path keys working.
- [ ] Implement deterministic empty-page generation with `atomic_write_text`.
- [ ] Route archive and milestone page outer markup through the shared shell; keep archive grouping, month tabs, summary states, model statuses, timeline order, sources, notes, and comparison values unchanged.
- [ ] Add stable `id="paper-{escaped canonical arXiv ID}"` to every archive row without changing external arXiv links.
- [ ] Make the paper heading `今天学什么` and remove user-facing `论文阅读`/`经典模型` primary-section labels in favor of registry labels only.
- [ ] Regenerate all ready milestone family pages from `config/milestone_models.yaml` and local notes, then generate the archive and empty pages.
- [ ] Run the temporary generation test; confirm it passes.
- [ ] Run a repository scan:

  ```powershell
  rg -n "论文阅读|经典模型|按论文主题与官方模型系列浏览" src docs/index.html docs/milestone-models docs/writings docs/journeys
  ```

  Expected: no obsolete primary/secondary copy in section pages; model-domain prose may still use “模型”.

- [ ] Delete `tests/test_site_generation.py` and `tests/tmp-site/` after resolving and printing their absolute paths; confirm `git status --short` contains no test artifacts and `README.md` is unchanged.
- [ ] Stage only Task 1–2 source/config/generated page files.
- [ ] Run `git diff --cached --stat` and `git diff --cached --check`.
- [ ] Commit: `feat: add four-section knowledge shell`

### Task 3: Add persistent secondary-sidebar collapse and retain existing interactions

**Files:**

- Create: `docs/assets/js/site-shell.js`
- Modify: `docs/assets/js/sidebar.js`
- Modify: `src/shared/site_shell.py`
- Modify: `docs/assets/css/site.css`
- Modify generated section pages under `docs/`
- Create locally, then delete: `tests/site-shell.test.js`

Move only global state out of `sidebar.js`. The resulting ownership is:

```text
site-shell.js  -> theme, mobile drawer, desktop context collapse, Escape drawer close
sidebar.js     -> archive year/month/hash, summary dialog, details actions, drag scroll
```

The collapse control contract is:

```html
<button class="context-collapse" type="button"
        aria-controls="context-navigation" aria-expanded="true"
        data-context-collapse>
  <span aria-hidden="true" data-context-collapse-icon>‹</span>
  <span class="visually-hidden">折叠二级导航</span>
</button>
```

State is stored only as `"true"` or `"false"` under `togos-secondary-sidebar-collapsed`; the root state is `html[data-secondary-collapsed="true"]`. On mobile the CSS ignores that desktop width state and the drawer exposes both navigation levels.

- [ ] Add a DOM-light Node test using `node:test` and a stubbed `localStorage`, `matchMedia`, and elements. Cover default expanded, stored collapse before initialization, click persistence, invalid value fallback, thrown storage read/write, mobile context remaining available, and Escape affecting only the mobile drawer.
- [ ] Run `node --test tests/site-shell.test.js`; confirm it fails because `site-shell.js` does not exist.
- [ ] Implement `site-shell.js` with guarded element lookup so empty/reader pages cannot crash it.
- [ ] Remove theme/mobile-drawer ownership from `sidebar.js`; keep its existing domain interaction blocks byte-for-byte where practical.
- [ ] Update shared shell markup with accessible collapse control and `id="context-navigation"`.
- [ ] Add desktop CSS tokens `--primary-width: 9rem`, `--secondary-width: 12rem`, `--secondary-collapsed-width: 2.75rem`; animate only the width/offset at at most `160ms`.
- [ ] Add `@media (max-width: 900px)` rules that make both sidebars one drawer and disregard the saved collapsed state.
- [ ] Add `@media (prefers-reduced-motion: reduce)` to remove shell transitions.
- [ ] Regenerate section pages so scripts and controls are consistent.
- [ ] Run `node --test tests/site-shell.test.js` and `python -m compileall -q src`.
- [ ] Delete `tests/site-shell.test.js`; confirm no tests are staged.
- [ ] Stage only collapse/site-shell assets, renderer changes, and regenerated pages.
- [ ] Run `git diff --cached --stat` and `git diff --cached --check`.
- [ ] Commit: `feat: add collapsible context navigation`

### Task 4: Publish a validated public search index

**Files:**

- Create: `src/shared/search_index.py`
- Modify: `src/papers/site.py`
- Modify: `src/milestones/publisher.py`
- Modify: `src/papers/collector.py`
- Modify: `.github/workflows/togos-daily.yml`
- Create: `docs/search-index.json`
- Modify: `docs/index.html`
- Create locally, then delete: `tests/test_search_index.py`

Use a strict, serializable document contract:

```python
@dataclass(frozen=True, slots=True)
class SearchDocument:
    id: str
    title: str
    url: str
    section: Literal["learning", "milestones", "writings", "journeys"]
    kind: Literal["paper", "model", "article"]
    published_at: str | None = None


def build_search_payload(
    documents: Iterable[SearchDocument], *, generated_on: datetime.date
) -> dict[str, object]: ...


def write_search_index(
    path: str | Path,
    documents: Iterable[SearchDocument],
    *,
    generated_on: datetime.date,
) -> None: ...
```

`build_search_payload` validates non-empty IDs/titles, registry section keys, supported kinds, ISO dates, root-relative-within-site URLs (no scheme, host, local path, or leading `/`), and duplicate IDs. It sorts deterministically by `(section, kind, id)` and omits `published_at` when absent.

- [ ] Add Python tests for the exact version-1 shape, deterministic ordering, JSON Unicode preservation, malformed date/URL/kind rejection, duplicate rejection, and atomic output.
- [ ] Add a future-article fixture with `public: false`; assert its title/source/Notion ID/local path are absent. Add a `public: true` article and assert only its normalized public fields enter.
- [ ] Add a paper present in both archive and ready summary catalog; assert one `paper:{id}` document whose URL is the summary URL. Assert a paper without a ready summary points to `index.html#paper-{id}`.
- [ ] Add ready/planned milestone fixtures; assert only ready family pages produce `model:{slug}` documents.
- [ ] Run `python -m unittest tests.test_search_index -v`; confirm it fails because the module/API is missing.
- [ ] Implement `SearchDocument`, validation, deterministic payload building, and atomic JSON writing via `atomic_write_text`.
- [ ] Add `papers.site.build_paper_search_documents(all_categories, summary_catalog)`; deduplicate by canonical paper ID before constructing `SearchDocument` values.
- [ ] Add `milestones.publisher.build_milestone_search_documents(catalog)` for ready families only.
- [ ] In unified generation, merge paper/model documents and write `docs/search-index.json` using the same `generated_on` value as the pages.
- [ ] Reserve an empty iterable adapter argument for future normalized public articles; do not scan Notion or private directories in V1.
- [ ] Update the daily workflow staging command to include `docs/search-index.json`; do not add secrets or change deployment topology.
- [ ] Run `python -m unittest tests.test_search_index -v` and regenerate the real site.
- [ ] Run a temporary URL verifier that splits every result URL at `#`, confirms the file exists under resolved `docs/`, and confirms any fragment exists in that HTML.
- [ ] Inspect `docs/search-index.json` for unique IDs and search for secret/path markers with `rg -ni "token|secret|notion|wsl|[A-Z]:\\\\" docs/search-index.json`.
- [ ] Delete all temporary search tests/fixtures/verifiers and confirm no tests are staged.
- [ ] Stage only Task 4 files, then run `git diff --cached --stat` and `git diff --cached --check`.
- [ ] Commit: `feat: publish public title index`

### Task 5: Add site-wide title search UI

**Files:**

- Create: `docs/assets/js/search-core.js`
- Create: `docs/assets/js/search.js`
- Modify: `src/shared/site_shell.py`
- Modify: `docs/assets/css/site.css`
- Modify generated section pages under `docs/`
- Create locally, then delete: `tests/search-core.test.js`
- Create locally, then delete: `tests/search-ui.test.js`

Keep ranking DOM-free and directly testable:

```javascript
function normalizeTitle(value) {
  return String(value ?? "").normalize("NFKC").toLocaleLowerCase().trim();
}

function searchTitles(documents, query, limit = 20) {
  // All whitespace-separated normalized tokens must occur in the title.
  // Rank: exact, prefix, earliest token position, newest published_at, stable id.
}
```

Expose the core as both `window.TogosSearchCore` and `module.exports` without importing a bundler. `search.js` owns the dialog and uses `body.dataset.siteRoot` for both `search-index.json` and result navigation.

- [ ] Add Node tests for NFKC Chinese matching, English case folding, whitespace normalization, multi-token AND semantics, exact-before-prefix ranking, earliest-position ranking, newest-date tie break, deterministic ID tie break, no match, and maximum 20 results.
- [ ] Add a small DOM-stub test for lazy first-open fetch, page-lifetime cache, invalid payload/load failure copy, arrow navigation, Enter activation, Escape close, and trigger focus restoration.
- [ ] Run both Node tests and confirm they fail before the modules exist.
- [ ] Implement `search-core.js` and make malformed input return an empty result rather than break unrelated controls.
- [ ] Add the shared visible trigger labeled exactly `搜索公开文章标题` and a native labeled `<dialog id="search-dialog">` with input, status, and listbox/result region.
- [ ] Implement `Ctrl+K`/`Cmd+K` opening, first-open fetch/cache, 20-result rendering, active descendant/keyboard state, and result labels from a localized section/kind map.
- [ ] Render empty-query guidance; render exact no-match copy `没有找到公开标题`; render exact failure copy `搜索索引加载失败，请刷新后重试` while leaving the page operational.
- [ ] Resolve the index as `new URL(`${siteRoot}search-index.json`, window.location.href)` and results as `new URL(`${siteRoot}${document.url}`, window.location.href)`; never use `/search-index.json`.
- [ ] Add visible focus, selected-result, dialog backdrop, 44px mobile targets, and dark-theme styles without decorative cards or gradients.
- [ ] Regenerate pages and run both Node tests.
- [ ] Delete both temporary test files and confirm no tests are staged.
- [ ] Stage only search client/shell/CSS/generated page files.
- [ ] Run `git diff --cached --stat` and `git diff --cached --check`.
- [ ] Commit: `feat: add site-wide title search`

### Task 6: Apply the approved minimal visual system and responsive behavior

**Files:**

- Modify: `docs/assets/css/site.css`
- Modify: `src/papers/site.py`
- Modify: `src/milestones/publisher.py`
- Modify: `src/shared/site_shell.py`
- Modify generated pages under `docs/`
- Create locally, then delete: `tests/test_rendered_accessibility.py`

The CSS token source must match the approved palette exactly:

```css
:root {
  --canvas: #fbfcfd;
  --surface: #ffffff;
  --ink: #111418;
  --muted: #66707b;
  --rule: #e3e7eb;
  --accent: #2258e6;
}

:root[data-theme="dark"] {
  --canvas: #0d1117;
  --surface: #11161d;
  --ink: #f2f4f7;
  --muted: #98a2ae;
  --rule: #28303a;
  --accent: #7ea2ff;
}
```

- [ ] Add a rendered-markup test for one document title, one `<h1>`, accessible names on theme/search/drawer/collapse controls, `aria-current`, search dialog labeling, unique IDs, and no external font URL or gradient token.
- [ ] Run it once against current generated output and retain the failing assertions as the visual cleanup checklist.
- [ ] Replace ornamental card surfaces/shadows/gradients with whitespace, typography, and hairline separators; keep the compass-blue marker as the only persistent accent.
- [ ] Use the platform stack `PingFang SC`, `Microsoft YaHei`, `Noto Sans SC`, system sans-serif; use system monospace only for dates/counts/IDs/key hints.
- [ ] Ensure `.page-content` derives its offset from shell variables and consumes remaining width instead of retaining the old combined fixed margin.
- [ ] At `max-width: 560px`, convert paper table rows into labeled stacked blocks while retaining ID, title, authors, and summary state; remove page-level horizontal overflow.
- [ ] Keep milestone comparison horizontal scrolling confined to its labeled internal viewport and preserve drag/keyboard controls.
- [ ] Align reader pages under `docs/notes/` and milestone `*-notes.html` with the same palette/typography without changing article content or summary manifests.
- [ ] Regenerate all source-owned outputs and run the rendered-markup test until it passes.
- [ ] Run `python -m compileall -q src` and a production generation command using the real archive/catalog.
- [ ] Delete `tests/test_rendered_accessibility.py`; confirm no test artifact is staged.
- [ ] Stage only visual/responsive source and generated artifacts.
- [ ] Run `git diff --cached --stat` and `git diff --cached --check`.
- [ ] Commit: `style: apply minimal knowledge compass design`

### Task 7: Browser QA, regression audit, and clean handoff

**Files:**

- Modify only files required by confirmed defects; do not make speculative additions.
- Create locally, then delete: browser screenshots and any ad hoc verifier under ignored `tests/`.

- [ ] Start a local HTTP server rooted at `docs/`; do not validate via `file://` because lazy `fetch` behavior differs.
- [ ] At widths `390`, `768`, `1280`, and `1440`, inspect `index.html`, `milestone-models/flux.html`, `writings/index.html`, and `journeys/index.html` for page-level horizontal overflow and content crowding.
- [ ] In light and dark themes, verify the exact minimal palette, active compass marker, focus visibility, no gradients/decorative card stacks, and readable contrast.
- [ ] Verify primary navigation labels/copy, archive counts/grouping, month tabs, year expansion, deep links, summary dialog, candidate states, model timeline, sources, comparison values, details actions, and drag/keyboard scrolling.
- [ ] Verify collapse persistence across navigation, storage-failure fallback, mobile drawer always showing both levels, and Escape not changing the saved desktop preference.
- [ ] Verify search from root and nested pages: Chinese substring, English case folding, multi-token query, exact/prefix/date ranking, 20-result cap, ready-summary anchor, archive-row anchor, model result, no-match copy, simulated index-load failure, arrows, Enter, Escape, and focus return.
- [ ] Emulate `prefers-reduced-motion: reduce`; verify shell/search transitions are removed.
- [ ] Run a final generated-output verifier for four links/correct active state/exact descriptions, unique search IDs, public-only fields, and resolvable files/fragments.
- [ ] Remove all temporary tests, fixtures, screenshots, local server logs, and coverage/cache outputs. Verify `git status --short --ignored` has no residual feature test artifacts that violate project rules.
- [ ] Run final checks:

  ```powershell
  python -m compileall -q src
  git diff --check origin/main..HEAD
  git status --short --branch
  git log --oneline --decorate -8
  ```

- [ ] If QA fixes were needed, stage only the affected feature files, run `git diff --cached --check`, and make one narrowly scoped English commit; otherwise make no empty cleanup commit.
- [ ] Confirm `README.md` is unchanged, no tests are tracked/staged, the branch is only ahead locally, and nothing was pushed.
- [ ] Use `superpowers:verification-before-completion` before reporting success.

## Commit sequence

1. `feat: add four-section knowledge shell`
2. `feat: add collapsible context navigation`
3. `feat: publish public title index`
4. `feat: add site-wide title search`
5. `style: apply minimal knowledge compass design`
6. Optional narrowly scoped fix commit only if browser QA discovers a real defect.

Each commit must be independently understandable, contain its corresponding generated outputs, exclude temporary tests, and pass `git diff --cached --check`.
