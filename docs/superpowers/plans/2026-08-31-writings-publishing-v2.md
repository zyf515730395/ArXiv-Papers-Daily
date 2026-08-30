# Writings Publishing V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `谈笑风生` into a fault-tolerant static publisher for repository-owned learning notes and book notes, including technical Markdown, local assets, navigation, and title search.

**Architecture:** Add an isolated `writings` domain that validates article bundles, renders each article independently, and stages a complete set of managed outputs. The existing site generator combines successful and retained article search documents with papers and milestone models, then promotes writings files and the search index through one rollback-capable transaction.

**Tech Stack:** Python 3.12, dataclasses, PyYAML, Python-Markdown, Bleach, `latex2mathml>=3.81,<4`, static HTML/CSS, `unittest`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-31-writings-publishing-design.md`

## Global Constraints

- Run `git pull --ff-only` before the first project-file modification in every resumed development session; stop if it fails or if unrelated uncommitted work is present.
- Do not modify `README.md`.
- `content/writings/` contains published bundles only; no drafts, credentials, external IDs, remote images, or fabricated sample articles.
- The bundle directory and `slug` use lowercase ASCII kebab-case and must match exactly.
- `kind` is exactly `learning-note` or `book-note`; `source` is exactly `original`, `notion`, or `wechat-reading`; `public` is exactly YAML boolean `true`.
- Markdown supports tables, fenced code, safe links, local raster images, H2/H3 TOC, `$...$`, and `$$...$$`; body H1 headings are invalid.
- New invalid articles are skipped; invalid updates retain all last known-good managed output; removing the entire bundle is the only unpublish operation.
- Per-article failures produce warnings and a successful process exit; unsafe manifests, transaction failures, and global serialization errors remain fatal.
- Local reports live at `build/reports/writings.json`; `/build/`, tests, fixtures, snapshots, and temporary sites are ignored and never committed.
- Every new or modified test-related file is deleted before its feature commit.
- Before every feature commit, inspect exact staged paths, run `git diff --cached --check`, and prove `README.md` is absent.

## File Structure

### Rules and source content

- Modify `AGENTS.md`: define ownership and naming rules for `content/writings/`, `src/writings/`, generated `docs/writings/`, and local-only `build/` output before creating those directories.
- Create `content/writings/AGENTS.md`: explain the bundle contract, asset boundary, deletion-based unpublish behavior, and prohibition on drafts/private metadata.

### Writings domain

- Create `src/writings/__init__.py`: export the public preparation and commit interfaces.
- Create `src/writings/models.py`: immutable article, manifest, issue, rendered article, build result, and prepared publication contracts.
- Create `src/writings/catalog.py`: strict front matter parsing, bundle discovery, manifest loading/serialization, and managed-path validation.
- Create `src/writings/rendering.py`: code-aware math conversion, sanitized Markdown, heading/TOC generation, asset validation, navigation, index pages, and article pages.
- Create `src/writings/publisher.py`: per-article isolation, last-known-good retention, staging, reporting, managed-file cleanup, and rollback-capable publication.

### Shared integration and generated site

- Modify `src/shared/search_index.py`: expose deterministic search-index serialization so it can participate in the publication transaction.
- Modify `src/shared/site_shell.py`: accept optional safe head markup for article-specific metadata without duplicating the shell.
- Modify `src/papers/site.py`: prepare writings, merge article search documents, and commit writings plus search atomically.
- Modify `pyproject.toml`: add `latex2mathml>=3.81,<4`.
- Modify `.gitignore`: ignore `/build/`.
- Modify `.github/workflows/togos-daily.yml`: stage `docs/writings/` and show degraded article warnings while leaving global failures fatal.
- Modify `docs/assets/css/site.css`: add editorial stream, filters, article typography, TOC, code/table overflow, images, and MathML styles.
- Regenerate `docs/writings/index.html`, `docs/writings/kind/*.html`, `docs/writings/manifest.json`, and `docs/search-index.json` through the public generator.

---

### Task 1: Article contracts, catalog validation, safe manifest, and reports

**Files:**
- Modify: `AGENTS.md`
- Modify: `.gitignore`
- Create: `content/writings/AGENTS.md`
- Create: `src/writings/__init__.py`
- Create: `src/writings/models.py`
- Create: `src/writings/catalog.py`
- Create: `tests/local/test_writings_catalog.py` (local only; delete before commit)

**Interfaces:**
- Produces: `WritingArticle`, `WritingIssue`, `ManifestArticle`, `WritingManifest`, `CatalogResult`, and `WritingCatalogError`.
- Produces: `discover_writings(source_root: Path, previous: WritingManifest) -> CatalogResult`.
- Produces: `load_manifest(path: Path, output_root: Path, *, generated_on: date) -> WritingManifest`.
- Produces: `serialize_manifest(manifest: WritingManifest) -> str` and `validate_managed_path(value: str, output_root: Path) -> PurePosixPath`.
- Consumes: only Python standard library and PyYAML.

- [ ] **Step 1: Extend workspace rules before creating directories**

Add these enforceable rules to `AGENTS.md`:

```markdown
## Writings 目录约定

- `content/writings/<slug>/index.md` 是公开文章唯一真源；本地图片只放在同 bundle 的 `assets/`。
- `src/writings/` 只包含文章校验、渲染与发布逻辑；跨主题能力留在 `src/shared/`。
- `docs/writings/` 只保存生成产物，受管范围以 `manifest.json` 为准，不手工编辑受管文件。
- `build/` 只保存本地报告和临时产物，必须保持忽略且不得提交。
```

Create `content/writings/AGENTS.md` with the exact front matter fields and these lifecycle rules: directory name equals slug, images stay below `assets/`, `public` must be true, removing the bundle unpublishes it, and drafts/private identifiers stay outside the repository. Add `/build/` to `.gitignore`; the directory-level `AGENTS.md` itself keeps the approved empty source root without fabricating an article.

- [ ] **Step 2: Write the failing catalog tests**

Create `tests/local/test_writings_catalog.py` using `tempfile.TemporaryDirectory` and table-driven cases. The core test fixture must be:

```python
VALID = """---
title: Diffusion Notes
slug: diffusion-notes
published_at: 2026-08-31
kind: learning-note
public: true
summary: A compact derivation.
tags: [diffusion, probability]
source: original
---
## Score matching
Body.
"""

class CatalogTests(unittest.TestCase):
    def test_valid_bundle_becomes_immutable_article(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "diffusion-notes"
            bundle.mkdir()
            (bundle / "index.md").write_text(VALID, encoding="utf-8")
            result = discover_writings(root, WritingManifest.empty(date(2026, 8, 31)))
            self.assertEqual([item.slug for item in result.articles], ["diffusion-notes"])
            self.assertEqual(result.articles[0].tags, ("diffusion", "probability"))
            self.assertEqual(result.issues, ())
```

Add explicit subtests that mutate the fixture and assert issue codes for `missing_front_matter`, `unknown_field`, `invalid_slug`, `slug_mismatch`, `invalid_date`, `invalid_kind`, `not_public`, `invalid_summary`, `invalid_tags`, `duplicate_tag`, `invalid_source`, missing `index.md`, and a root entry that is not a valid bundle directory. Assert that one invalid bundle does not remove a valid neighbor from `CatalogResult.articles`.

Add manifest tests asserting rejection of absolute paths, `..`, backslashes, duplicate paths, unsupported version, malformed records, a path outside `docs/writings/`, and non-string entries. Assert round-trip serialization sorts article keys and managed paths deterministically.

- [ ] **Step 3: Run the catalog tests and confirm the intended failure**

Run:

```powershell
python -m unittest tests.local.test_writings_catalog -v
```

Expected: import failure for `writings.catalog` because the new domain does not exist yet.

- [ ] **Step 4: Implement immutable domain contracts**

In `src/writings/models.py`, define frozen, slotted dataclasses with these fields:

```python
WritingArticle(
    slug: str,
    title: str,
    published_at: date,
    kind: Literal["learning-note", "book-note"],
    summary: str,
    tags: tuple[str, ...],
    source: Literal["original", "notion", "wechat-reading"],
    source_path: Path,
    bundle_root: Path,
    body: str,
)
WritingIssue(source: str, code: str, message: str)
ManifestArticle(
    source: str,
    title: str,
    published_at: str,
    kind: str,
    summary: str,
    tags: tuple[str, ...],
    page: str,
    assets: tuple[str, ...],
)
WritingManifest(
    version: int,
    generated_at: str,
    articles: Mapping[str, ManifestArticle],
    managed_files: tuple[str, ...],
)
CatalogResult(articles: tuple[WritingArticle, ...], issues: tuple[WritingIssue, ...])
```

`WritingManifest.empty(generated_on)` returns version 1, the ISO date, an empty immutable mapping, and no managed paths. Use `MappingProxyType` when freezing manifest mappings.

- [ ] **Step 5: Implement strict discovery and front matter parsing**

In `src/writings/catalog.py`, compile these exact patterns and sets:

```python
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONT_MATTER_PATTERN = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)
REQUIRED_FIELDS = {"title", "slug", "published_at", "kind", "public", "summary", "tags", "source"}
SUPPORTED_KINDS = {"learning-note", "book-note"}
SUPPORTED_SOURCES = {"original", "notion", "wechat-reading"}
```

Parse YAML with `yaml.safe_load`, require an exact field set, reject booleans where a string is expected, parse `published_at` with `date.fromisoformat`, reject summary newlines and `<`/`>`, and preserve normalized tag order. Discovery sorts directory entries by name, ignores only the directory contract file `AGENTS.md`, and converts every bundle-level failure into a safe repository-relative `WritingIssue` without including body text or an absolute path.

- [ ] **Step 6: Implement manifest boundary validation and deterministic serialization**

`validate_managed_path` must reject empty, absolute, backslash-containing, dot, parent-traversing, non-normalized, and `manifest.json` values. Resolve `output_root / PurePosixPath(value)` and require it to remain below `output_root`. `load_manifest` returns an empty manifest only when the file is absent; any existing malformed manifest raises `WritingCatalogError` and never falls back silently.

Serialize compact UTF-8 JSON with `ensure_ascii=False`, sorted article keys, sorted unique managed files, and one trailing newline. Never serialize local absolute paths or article bodies.

- [ ] **Step 7: Run the focused tests and static checks**

Run:

```powershell
python -m unittest tests.local.test_writings_catalog -v
python -m compileall -q src
```

Expected: all catalog tests pass and compilation exits 0.

- [ ] **Step 8: Remove local-only tests and commit the first feature**

Delete `tests/local/test_writings_catalog.py` and any `__pycache__` created below `tests/`. Then run:

```powershell
git status --short
git add AGENTS.md .gitignore content/writings src/writings
git diff --cached --name-only
git diff --cached --check
git diff --cached -- README.md
git commit -m "feat: validate writing article bundles"
```

Expected: the staged list contains no `tests/`, `build/`, generated site, or `README.md` paths.

---

### Task 2: Technical Markdown, MathML, local assets, and article rendering

**Files:**
- Modify: `src/writings/models.py`
- Create: `src/writings/rendering.py`
- Modify: `src/shared/site_shell.py`
- Create: `tests/local/test_writings_rendering.py` (local only; delete before commit)

**Interfaces:**
- Consumes: `WritingArticle` from Task 1 and `render_site_page` / `render_section_intro` from `shared.site_shell`.
- Produces: `RenderedArticle(html: str, toc: tuple[TocEntry, ...], assets: tuple[AssetCopy, ...])`.
- Produces: `render_article(article, *, output_file: Path, output_root: Path) -> RenderedArticle`.
- Produces: `render_article_page(article, rendered, *, output_file, output_root) -> str`.
- Produces: `render_writings_index(records, *, active_filter, output_file, output_root) -> str`.

- [ ] **Step 1: Write failing renderer tests**

Create a temporary bundle with `assets/plot.png`, construct a `WritingArticle`, and assert:

```python
rendered = render_article(article, output_file=page, output_root=site_root)
self.assertIn('<math xmlns="http://www.w3.org/1998/Math/MathML"', rendered.html)
self.assertIn('<code>$not_math$</code>', rendered.html)
self.assertIn('<table>', rendered.html)
self.assertIn('src="assets/diffusion-notes/plot.png"', rendered.html)
self.assertEqual([(item.level, item.label) for item in rendered.toc], [(2, "推导"), (3, "结论")])
```

Add cases for inline and block math, escaped dollar signs, invalid LaTeX, fenced code, duplicate H2/H3 headings, a body H1, raw script/event attributes, external links receiving `rel="noopener noreferrer"`, allowed fragment/site-relative/mailto links, missing assets, `..`, absolute paths, remote/data URLs, unsupported extensions, backslashes, and an escaping symlink when the platform permits symlinks.

Add page assertions that the article sidebar contains a back link plus H2/H3 TOC, does not repeat the `谈笑风生` title/description, and uses nested site-root-relative asset links. Add index assertions for descending date then ascending slug, localized kinds, tag counts, static filter URLs, and the approved empty state.

- [ ] **Step 2: Run renderer tests and verify failure**

Run:

```powershell
python -m unittest tests.local.test_writings_rendering -v
```

Expected: import failure for `writings.rendering`.

- [ ] **Step 3: Add static MathML dependency and rendering contracts**

Add `latex2mathml>=3.81,<4` to `pyproject.toml`. Extend `models.py` with:

```python
TocEntry(level: Literal[2, 3], anchor: str, label: str)
AssetCopy(source: Path, destination: str)
RenderedArticle(html: str, toc: tuple[TocEntry, ...], assets: tuple[AssetCopy, ...])
```

Define `WritingRenderError(code: str, message: str)` so the publisher can preserve stable issue codes without exposing stack traces.

Install the updated project before running renderer tests:

```powershell
python -m pip install -e .
```

- [ ] **Step 4: Implement code-aware math tokenization**

In `src/writings/rendering.py`, reject the reserved prefix `TOGOSPROTECTEDTOKEN` in source text. Mask fenced code blocks and variable-length inline backtick spans first, convert non-code `$...$` and `$$...$$` with `latex2mathml.converter.convert(..., display=...)`, then restore the original Markdown code tokens before Markdown parsing. Store converted MathML under protected text tokens and restore it only after Bleach sanitizes author-controlled HTML.

Reject unclosed math delimiters and converter exceptions as `WritingRenderError("invalid_math", "Invalid LaTeX expression")`; do not include the formula or converter traceback in the public/local error message.

- [ ] **Step 5: Implement deterministic headings, sanitization, and assets**

Reject any body line matching `^\s{0,3}#\s+`. Use Python-Markdown `extra`, `sane_lists`, and `toc` with a custom slugifier that applies Unicode NFKC, transliterates ASCII where possible, falls back to `section`, and adds `-2`, `-3` suffixes for duplicates. Extract only H2/H3 tokens into `TocEntry` records.

Allow only semantic article tags (`p`, headings, lists, blockquote, pre/code, table elements, `a`, `img`, `hr`, `br`, `em`, `strong`) and narrow attributes (`href`, `title`, `rel`, `target`, `src`, `alt`, and validated code language classes). Strip raw HTML outside the allow-list. Normalize external HTTP(S) links to `target="_blank" rel="noopener noreferrer"` while preserving safe local links.

For each image, require a relative POSIX path below `assets/`, resolve it inside the bundle, reject an escaping symlink, require a regular file and one of `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.avif`, then rewrite to `assets/<article-slug>/<relative-under-assets>` and emit one deduplicated `AssetCopy`.

- [ ] **Step 6: Render article, stream, filter, and context navigation HTML**

Use `html.escape` for all metadata. `render_article_page` emits one page-level H1, date, localized kind, tag links, and `.writing-body` with a `52rem` reading measure. Its secondary navigation begins with `← 全部文章` and nests H3 links under the preceding H2; no section identity appears in the sidebar.

`render_writings_index` sorts `(published_at desc, slug asc)`, emits semantic `<article>` rows instead of cards, and accepts `active_filter=None`, `("kind", value)`, or `("tag", value)`. The index sidebar links to all articles, both kinds, and all tags with counts. With no records it retains `公开的学习笔记和读书笔记会在这里汇集。`.

Extend `render_site_page(..., head_content: str = "")`; inject only caller-generated trusted head markup immediately before the stylesheet links. Existing callers remain byte-for-byte equivalent when `head_content` is empty.

- [ ] **Step 7: Run focused and shell regression tests**

Run:

```powershell
python -m unittest tests.local.test_writings_rendering -v
python -m compileall -q src
python -c "from papers.site import generate_site; print(generate_site.__name__)"
```

Expected: renderer tests pass, compilation exits 0, and the existing module remains importable.

- [ ] **Step 8: Remove local tests and commit the second feature**

Delete the renderer test and test caches, then stage only renderer-related files:

```powershell
git add pyproject.toml src/writings/models.py src/writings/rendering.py src/shared/site_shell.py
git diff --cached --name-only
git diff --cached --check
git diff --cached -- README.md
git commit -m "feat: render technical writing articles"
```

Expected: no test, generated-output, report, or README file is staged.

---

### Task 3: Fault-tolerant publication, global search, UI, workflow, and generated output

**Files:**
- Modify: `src/writings/models.py`
- Create: `src/writings/publisher.py`
- Modify: `src/writings/__init__.py`
- Modify: `src/shared/search_index.py`
- Modify: `src/papers/site.py`
- Modify: `docs/assets/css/site.css`
- Modify: `.github/workflows/togos-daily.yml`
- Replace through generator: `docs/writings/index.html`
- Create through generator: `docs/writings/kind/learning-note.html`
- Create through generator: `docs/writings/kind/book-note.html`
- Create through generator: `docs/writings/manifest.json`
- Regenerate through generator: `docs/search-index.json`
- Create: `tests/local/test_writings_publisher.py` (local only; delete before commit)
- Create: `tests/local/test_site_integration.py` (local only; delete before commit)
- Create: `local-site/` visual test output (local only; delete before commit)

**Interfaces:**
- Consumes: Task 1 catalog/manifest interfaces and Task 2 rendering interfaces.
- Produces: `prepare_writings_publication(source_root, output_root, report_path, generated_on) -> PreparedPublication`.
- Produces: `commit_writings_and_search(prepared, search_index_path, search_content) -> None`.
- Produces: `serialize_search_index(documents, *, generated_on) -> str` while preserving `write_search_index` as a wrapper.
- Produces: `WritingBuildResult` with published, retained, skipped, removed, issues, and article search documents.

- [ ] **Step 1: Write failing publisher and transaction tests**

Build tests entirely under temporary directories. Use a first successful publish followed by mutations, and assert these exact outcomes:

```python
first = prepare_writings_publication(source, writings_out, report, date(2026, 8, 31))
first_search = serialize_search_index(first.result.search_documents, generated_on=date(2026, 8, 31))
commit_writings_and_search(first, search_path, first_search)
previous_page = (writings_out / "stable-slug.html").read_bytes()

(source / "stable-slug" / "assets" / "plot.png").unlink()
second = prepare_writings_publication(source, writings_out, report, date(2026, 9, 1))
self.assertEqual(second.result.retained, ("stable-slug",))
second_search = serialize_search_index(second.result.search_documents, generated_on=date(2026, 9, 1))
commit_writings_and_search(second, search_path, second_search)
self.assertEqual((writings_out / "stable-slug.html").read_bytes(), previous_page)
```

Add cases for valid + invalid new articles, retained metadata and search record, explicit bundle deletion, `public:false`, stale tag page removal, preservation of an unmanaged sentinel file, deterministic report schema, safe issue content, missing retained files as fatal, malformed/unsafe prior manifest as fatal, duplicate output routes as fatal, and output/report/staging paths outside their allowed roots.

Inject a rename failure after writings promotion and assert the old writings files plus old search index are restored byte-for-byte and temporary/backup directories are removed. Assert a degraded per-article build commits successfully and reports `status: degraded`.

- [ ] **Step 2: Write failing unified-site regression tests**

Create a temporary site root, copy the minimum paper/milestone fixtures, call `generate_site` with explicit `writings_source_root` and `writings_report_path`, and assert one valid article produces all routes and exactly one `article:<slug>` search record. Run a zero-article case and assert existing paper/model record counts and URLs remain unchanged.

Add a baseline assertion against the repository’s current public search index:

```python
payload = json.loads(Path("docs/search-index.json").read_text(encoding="utf-8"))
self.assertEqual(len(payload["documents"]), 8759)
self.assertEqual(sum(item["kind"] == "paper" for item in payload["documents"]), 8758)
self.assertEqual(sum(item["kind"] == "model" for item in payload["documents"]), 1)
```

- [ ] **Step 3: Run publisher/integration tests and confirm failure**

Run:

```powershell
python -m unittest tests.local.test_writings_publisher tests.local.test_site_integration -v
```

Expected: import failure for the publisher APIs and missing `generate_site` keywords.

- [ ] **Step 4: Implement preparation, retention, reports, and search documents**

Extend `models.py` with:

```python
WritingBuildResult(
    published: tuple[str, ...],
    retained: tuple[str, ...],
    skipped: tuple[str, ...],
    removed: tuple[str, ...],
    issues: tuple[WritingIssue, ...],
    search_documents: tuple[SearchDocument, ...],
)
PreparedPublication(
    staging_root: Path,
    output_root: Path,
    previous_manifest: WritingManifest,
    next_manifest: WritingManifest,
    result: WritingBuildResult,
)
```

`prepare_writings_publication` validates all roots first, loads the prior manifest, discovers source bundles, and processes each article inside its own `try` boundary. Successful articles write their page and assets into a sibling staging directory. A failed known slug copies every prior manifest-listed page/asset into staging byte-for-byte and retains prior public metadata; a failed new slug is omitted. A missing retained file raises a global `WritingPublishError`.

Regenerate index/kind/tag pages from successful plus retained records. Add every generated page and copied asset except `manifest.json` to `managed_files`. Build one `SearchDocument(id=f"article:{slug}", url=f"writings/{slug}.html", section="writings", kind="article")` per successful or retained record.

Write `build/reports/writings.json` with compact safe JSON, counts, status, repository-relative sources, stable issue codes, and sanitized messages. Use `atomic_write_text`; create the report parent only after confirming it remains under the project-local `build/` root.

- [ ] **Step 5: Implement managed-file and search transaction**

Add `serialize_search_index` to `src/shared/search_index.py` and implement `write_search_index` as `atomic_write_text(path, serialize_search_index(...))` so existing callers keep their API.

`commit_writings_and_search` performs this ordered transaction:

1. Validate every old/new managed path again.
2. Copy the current versions of every path that may change or be removed, plus `manifest.json` and `search-index.json`, into a sibling backup directory.
3. Promote staged managed files with `os.replace`, then promote `manifest.json`, then the prepared search-index file.
4. Remove only `previous.managed_files - next.managed_files`.
5. On any exception, restore backed-up files, remove paths that did not exist before, and re-raise `WritingPublishError`.
6. Delete staging/backup paths only after verifying their resolved locations remain within the configured parent.

Unmanaged files below `docs/writings/` are never enumerated for removal and remain untouched.

- [ ] **Step 6: Integrate the unified generator and CI**

Change `generate_site` to accept keyword defaults:

```python
writings_source_root: str | Path = PROJECT_ROOT / "content" / "writings"
writings_report_path: str | Path = PROJECT_ROOT / "build" / "reports" / "writings.json"
```

Remove writings from `empty_pages` but keep journeys. Prepare writings before final search serialization, merge `prepared.result.search_documents` after papers and models, call `serialize_search_index`, then commit writings and search together. Print/log one `::warning`-compatible line per issue and a final published/retained/skipped/removed summary; do not raise for degraded status.

Update the GitHub workflow commit step to include:

```yaml
git add docs/writings docs/search-index.json
```

Do not stage `build/`. Global exceptions still stop the workflow because the generator returns non-zero.

- [ ] **Step 7: Add editorial and technical-content CSS**

Append a dedicated writings section using existing design tokens. Required selectors include `.writings-hero`, `.writing-stream`, `.writing-entry`, `.writing-meta`, `.writing-tags`, `.writing-article`, `.writing-header`, `.writing-body`, `.writing-toc-h3`, `.writing-filter-heading`, and `.math-display`.

Set `.writing-body { max-width: 52rem; }`; give `pre` and table wrappers `overflow-x: auto; max-width: 100%`; make images `max-width: 100%; height: auto`; style MathML to inherit text color; keep typography ratios within the existing shell scale. At `900px` and `560px`, preserve drawer/collapsed-sidebar behavior and prevent page-level horizontal overflow. Respect `prefers-reduced-motion`.

- [ ] **Step 8: Run automated verification and generate the no-article public output**

Run:

```powershell
python -m unittest tests.local.test_writings_publisher tests.local.test_site_integration -v
python -m compileall -q src
python -c "from papers.site import generate_site; generate_site('docs/togos-papers.json', 'docs/index.html', 'data/arxiv-candidates.json', 'config/milestone_models.yaml', output_root='docs', search_index_path='docs/search-index.json', writings_source_root='content/writings', writings_report_path='build/reports/writings.json')"
```

The command reads existing `docs/togos-papers.json` and never calls arXiv. Expected generated state: empty writings index, both empty kind routes, manifest version 1, no article search records, and exactly 8,759 total search documents.

- [ ] **Step 9: Perform browser verification at three widths**

Serve the generated site locally and inspect writings index, both kind pages, and one temporary valid article at 390, 768, and 1440 pixels. Verify light/dark themes, primary drawer, secondary collapse, keyboard focus, TOC links, table/code scrolling, image sizing, MathML, global title search, and absence of horizontal page overflow.

Store screenshots only under `local-site/` or another ignored temp directory. Delete the temporary source article, regenerate the approved zero-article state, and confirm no fabricated article or screenshot remains in Git status.

- [ ] **Step 10: Run final regression and safety checks**

Run:

```powershell
python -m unittest tests.local.test_writings_publisher tests.local.test_site_integration -v
python -m compileall -q src
python -c "import json; p=json.load(open('docs/search-index.json', encoding='utf-8')); assert len(p['documents']) == 8759; assert not any(d['kind'] == 'article' for d in p['documents'])"
git diff --check
```

Inspect `build/reports/writings.json` to confirm it contains no body, absolute path, credential, source ID, environment value, or traceback.

- [ ] **Step 11: Remove all local-only artifacts and commit the third feature**

Delete `tests/local/test_writings_publisher.py`, `tests/local/test_site_integration.py`, test caches, `local-site/`, and the local `build/` directory. Then stage only production source, configuration, workflow, CSS, and generated public output:

```powershell
git add src/writings src/shared/search_index.py src/papers/site.py docs/assets/css/site.css .github/workflows/togos-daily.yml docs/writings docs/search-index.json
git diff --cached --name-only
git diff --cached --check
git diff --cached -- README.md
git commit -m "feat: publish writings with graceful fallback"
```

Expected: no `tests/`, `build/`, `local-site/`, fake article, private metadata, or README path is staged.

- [ ] **Step 12: Final verification, review, push, and next-version handoff**

Run the full deterministic generator again from a clean working tree, verify `git status --short`, inspect the complete branch diff, and use `superpowers:requesting-code-review`. Resolve all valid findings, rerun verification, and use `superpowers:verification-before-completion` before claiming success.

Push `main` only after the commits and generated outputs pass. Then inventory the remaining approved roadmap (`Notion` import, WeChat Reading + WSL summarization, and the `跑得还快` world map), choose the smallest dependency-free next slice, and begin its required design cycle without asking the user to choose among recommended defaults.
