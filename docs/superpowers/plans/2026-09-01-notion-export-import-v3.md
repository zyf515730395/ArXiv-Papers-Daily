# Notion Export Import V3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a safe offline workflow that inspects a Notion Markdown & CSV export, previews selected pages as valid writing bundles, and idempotently applies only state-trusted, unedited bundles to `content/writings/`.

**Architecture:** A new `writings.importers` adapter package owns export inventory, private plans/state, Notion Markdown conversion, preview, and guarded promotion. It calls a small public single-bundle validation surface in the existing writings domain, while the V2 publisher remains independent and never imports adapter code. All input snapshots, plans, state, previews, reports, fixtures, and tests remain ignored/local; only validated public bundles can cross into `content/writings/`.

**Tech Stack:** Python 3.12 standard library (`argparse`, `contextlib`, `dataclasses`, `hashlib`, `html.parser`, `json`, `pathlib`, `shutil`, `tempfile`, `zipfile`), PyYAML 6, existing Python-Markdown/Bleach writings renderer, `unittest` for local-only tests.

**Spec:** `docs/superpowers/specs/2026-09-01-notion-export-import-design.md`

## Global Constraints

- Run `git pull --ff-only` successfully before modifications; stop without force/rebase/reset if it fails.
- Extend directory rules before creating `src/writings/importers/`; keep archive, conversion, plan, state, promotion, and CLI responsibilities separated exactly as the spec defines.
- Accept only one official-style `.zip` archive or one readable directory; never follow symlinks, Windows junctions, or reparse points.
- Keep public site generation credential-free and independent from importers; no importer writes `docs/`.
- Keep plans, state, preview, extraction, reports, export fixtures, and tests below ignored `build/` or `tests/`; delete all tests/fixtures/reports/previews/caches before every commit.
- Never commit Notion IDs, credentials, absolute paths, state, plan, report, preview, fake articles, test artifacts, or README changes.
- Never infer `published_at`, `kind`, `summary`, or `tags`; every selected article must contain reviewed values satisfying the existing V2 front matter contract.
- There is no `--force`; occupied untrusted slugs and human-edited state-trusted bundles are conflicts.
- A candidate failure is isolated; unsafe roots, invalid archives/plans/state, ambiguous recovery residue, and failed rollback are global fatal errors.
- Commit messages are concise English and each commit contains one independently reviewable feature.

---

### Task 1: Safe export inventory and deterministic inspect plan

**Files:**
- Modify: `AGENTS.md`
- Create: `src/writings/importers/AGENTS.md`
- Create: `src/writings/importers/__init__.py`
- Create: `src/writings/importers/models.py`
- Create: `src/writings/importers/archive.py`
- Create: `src/writings/importers/planner.py`
- Create: `src/writings/importers/notion.py`
- Local test only, then delete: `tests/local/test_notion_inspect.py`
- Local fixtures only, then delete: `build/notion-import/test-inspect/`

**Interfaces:**
- Consumes: existing `shared.rendering.atomic_write_text(path, content)` and PyYAML safe load/dump.
- Produces in `models.py`:
  - `ImportLimits(max_members: int = 10_000, max_file_bytes: int = 67_108_864, max_total_bytes: int = 1_073_741_824)`.
  - `ExportFile(relative_path: PurePosixPath, source_path: Path, size: int, sha256: str)`.
  - `ExportInventory(root: Path, files: Mapping[str, ExportFile], markdown_paths: tuple[str, ...], csv_paths: tuple[str, ...], fingerprint: str)`; copy mappings into `MappingProxyType` in `__post_init__`.
  - `ImportArticlePlan(source_ref: str, detected_title: str, include: bool, slug: str, title: str, published_at: str | None, kind: str | None, summary: str | None, tags: tuple[str, ...])`.
  - `ImportPlan(version: int, source: Literal["notion-export"], export_fingerprint: str, articles: tuple[ImportArticlePlan, ...])`.
  - `ImportIssue(source: str, code: str, message: str)` and `NotionImportError(code: str, message: str)`; expose `.code` and `.message`, and render `str(error)` as `<code>: <safe message>` containing no absolute path or page ID.
- Produces in `archive.py`:
  - `DEFAULT_LIMITS = ImportLimits()`.
  - `open_export(source: str | Path, work_root: str | Path, limits: ImportLimits = DEFAULT_LIMITS) -> ContextManager[ExportInventory]`.
- Produces in `planner.py`:
  - `load_import_plan(path: str | Path) -> ImportPlan`.
  - `serialize_import_plan(plan: ImportPlan) -> str` with deterministic YAML field and article ordering.
  - `inspect_export(inventory: ExportInventory, previous: ImportPlan | None = None) -> ImportPlan`.
  - `write_import_plan(path: str | Path, plan: ImportPlan) -> None` using atomic replacement inside canonical ignored `build/notion-import/`.
  - `redact_source_ref(source_ref: str) -> str`; replace recognized 32-hex IDs with `[notion-id]` and return only a normalized relative reference.
- Produces in `notion.py`: `build_parser() -> argparse.ArgumentParser`, `run(argv: Sequence[str] | None = None) -> int`, and `main() -> None`; Task 1 implements only `inspect EXPORT --plan PLAN`.

- [ ] **Step 1: Extend rules before creating the importer directory**

Add these root rules before any importer source exists:

```markdown
## Writings 导入器约定

- `src/writings/importers/` 只负责把外部导出物转换为标准 writing bundle；发布器不得反向依赖 importer。
- 导入计划、私有映射、预览、报告和解压内容只放在已忽略的 `build/notion-import/` 或 `build/reports/`，不得进入 `content/` 或 `docs/`。
- importer 只能通过显式 apply 修改 `content/writings/<slug>/`，不得生成、提交或推送站点产物。
```

Create `src/writings/importers/AGENTS.md` with the module ownership listed under **Files**, path/privacy rules, no-network rule, state-before-content rollback rule, and local-test deletion rule.

- [ ] **Step 2: Write archive RED tests**

Create a local `unittest` module with helpers that write ZIP members without extracting them:

```python
class ExportArchiveTests(unittest.TestCase):
    def test_zip_and_directory_have_identical_inventory(self):
        with open_export(self.zip_path, self.work_root) as zipped:
            zipped_value = inventory_signature(zipped)
        with open_export(self.directory, self.work_root) as directory:
            directory_value = inventory_signature(directory)
        self.assertEqual(zipped_value, directory_value)

    def test_rejects_traversal_absolute_drive_unc_and_backslash_members(self):
        for member in ("../secret.md", "/root.md", "C:/drive.md", "//server/share.md", "a\\b.md"):
            with self.subTest(member=member):
                self.assert_archive_error({member: b"secret"}, "unsafe_archive")

    def test_rejects_duplicate_normalized_and_casefolded_members(self):
        self.assert_archive_error({"A.md": b"one", "a.md": b"two"}, "unsafe_archive")

    def test_rejects_symlink_special_encrypted_and_oversized_members(self):
        for fixture in (linked_zip(), special_zip(), encrypted_zip(), oversized_zip()):
            with self.subTest(fixture=fixture.name):
                with self.assertRaisesRegex(NotionImportError, "unsafe_archive"):
                    with open_export(fixture, self.work_root, self.small_limits):
                        self.fail("unsafe member was inventoried")

    def test_directory_rejects_symlink_or_junction_entries(self):
        make_directory_link(self.directory / "linked", self.external)
        with self.assertRaisesRegex(NotionImportError, "unsafe_archive"):
            with open_export(self.directory, self.work_root):
                self.fail("linked directory was inventoried")

    def test_context_removes_only_its_verified_temporary_extraction(self):
        sentinel = self.work_root / "sentinel"
        sentinel.mkdir(parents=True)
        with open_export(self.zip_path, self.work_root) as inventory:
            extraction = inventory.root
            self.assertTrue(extraction.is_dir())
        self.assertFalse(extraction.exists())
        self.assertTrue(sentinel.is_dir())
```

The valid fixture contains `Notes/Alpha.md`, `Notes/Alpha/image.png`, and `Database.csv`. Assert sorted POSIX paths, exactly one Markdown path, one CSV path, SHA-256 file digests, and identical inventory fingerprints for ZIP/directory inputs.

- [ ] **Step 3: Run archive tests and verify RED**

Run:

```powershell
python -m unittest -v tests.local.test_notion_inspect.ExportArchiveTests
```

Expected: import failure because `writings.importers.archive` and its contracts do not exist.

- [ ] **Step 4: Implement immutable models and safe export inventory**

Implement canonical member validation before extraction. Use `PurePosixPath` plus `PureWindowsPath`, reject `\`, `.`, `..`, absolute/drive/UNC/device aliases, duplicate normalized/case-folded keys, ZIP external attributes describing links/special files, encryption, and configured limits. Hash sorted `(relative_path, size, file_sha256)` records for the inventory fingerprint.

For directory inputs, reject links/junctions at every traversed component and do not call a recursive API that follows them. For ZIP inputs, extract each prevalidated regular member with bounded streaming into a newly created directory under resolved `build/notion-import/runs/`, then remove only that verified directory in the context manager's `finally` block.

- [ ] **Step 5: Run archive tests and verify GREEN**

Run the command from Step 3. Expected: all archive tests pass and no run directory remains.

- [ ] **Step 6: Write plan/inspect RED tests**

Add:

```python
class InspectPlanTests(unittest.TestCase):
    def test_inspect_defaults_every_candidate_to_excluded(self):
        plan = inspect_export(self.inventory)
        self.assertTrue(plan.articles)
        self.assertTrue(all(article.include is False for article in plan.articles))
        self.assertTrue(all(article.published_at is None for article in plan.articles))

    def test_title_prefers_single_leading_h1_then_sanitized_filename(self):
        plan = inspect_export(self.inventory)
        values = {article.source_ref: article for article in plan.articles}
        self.assertEqual(values["Notes/Alpha.md"].detected_title, "Alpha heading")
        self.assertEqual(values["Notes/Beta Note.md"].detected_title, "Beta Note")

    def test_slug_suggestion_is_normalized_unique_and_deterministic(self):
        first = inspect_export(self.inventory)
        second = inspect_export(self.inventory)
        self.assertEqual([item.slug for item in first.articles], [item.slug for item in second.articles])
        self.assertEqual(len({item.slug for item in first.articles}), len(first.articles))

    def test_reinspect_preserves_reviewed_fields_for_exact_source_match(self):
        reviewed = with_reviewed_alpha(inspect_export(self.inventory))
        refreshed = inspect_export(self.inventory, reviewed)
        self.assertEqual(find_article(refreshed, "Notes/Alpha.md"), find_article(reviewed, "Notes/Alpha.md"))

    def test_plan_rejects_unknown_fields_invalid_types_duplicates_and_absolute_refs(self):
        for payload in invalid_plan_payloads():
            with self.subTest(payload=payload["case"]), self.assertRaises(NotionImportError):
                load_import_plan(write_plan_payload(self.root, payload["value"]))

    def test_serialization_is_stable_and_contains_no_absolute_input_path(self):
        text = serialize_import_plan(inspect_export(self.inventory))
        self.assertEqual(text, serialize_import_plan(load_plan_text(self.root, text)))
        self.assertNotIn(str(self.export_root.resolve()), text)

    def test_cli_inspect_writes_only_below_build_and_prints_redacted_summary(self):
        code, stdout, stderr = run_cli("inspect", self.zip_path, "--plan", self.plan_path)
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Discovered 2 Markdown pages; selected 0", stdout)
        self.assertTrue(self.plan_path.is_file())
        self.assertNotIn("0123456789abcdef0123456789abcdef", stdout)
```

Assert the generated plan version/source/fingerprint, `include: false`, detected/public title, suggested slug, null reviewed fields, empty tags, stable source ordering, and preservation of manually reviewed fields only when `source_ref` remains exact.

- [ ] **Step 7: Run plan tests and verify RED**

Run:

```powershell
python -m unittest -v tests.local.test_notion_inspect.InspectPlanTests
```

Expected: failures for missing plan parser/serializer and CLI behavior.

- [ ] **Step 8: Implement deterministic plans and inspect CLI**

Use strict YAML: top-level fields must be exactly `version`, `source`, `export_fingerprint`, `articles`; article fields must exactly match `ImportArticlePlan`. Validate SHA-256 syntax, normalized relative POSIX `source_ref`, unique source refs/slugs, strict booleans, supported kind/date/tag syntax when values are present, and `include: true` completeness only later during preview.

Detect one source leading H1 without changing body. Suggested slugs use Unicode NFKC, lowercase ASCII alphanumerics, kebab separators, and a deterministic eight-hex source-ref hash when the ASCII base is empty or collides. `inspect` may read an existing plan to preserve reviewed fields but always replaces its export fingerprint and source inventory.

The CLI catches `NotionImportError`, prints `Notion inspect failed: <safe message>` to stderr, returns `2` for global invalid input, and returns `0` with `Discovered N Markdown pages; selected 0; plan: build/notion-import/plan.yaml` for success.

- [ ] **Step 9: Run all Task 1 verification**

Run:

```powershell
python -m unittest -v tests.local.test_notion_inspect
python -m compileall -q src
python -m writings.importers.notion inspect build/notion-import/test-inspect/valid.zip --plan build/notion-import/plan.yaml
git diff --check
```

Expected: all tests pass; CLI produces a deterministic ignored plan; compile and diff checks exit `0`; no `content/` or `docs/` changes.

- [ ] **Step 10: Remove local artifacts and commit Task 1**

Delete `tests/local/test_notion_inspect.py`, test package/cache files, `build/notion-import/test-inspect/`, the generated plan/run directories, and generated reports. Verify `git status --short --ignored` contains no unignored test/output files. Stage only rule and production source files, run `git diff --cached --check`, inspect `git diff --cached --name-status`, and commit:

```powershell
git commit -m "feat: inspect notion exports safely"
```

---

### Task 2: Notion Markdown conversion and local preview

**Files:**
- Modify: `src/writings/catalog.py`
- Modify: `src/writings/__init__.py`
- Modify: `src/writings/importers/models.py`
- Create: `src/writings/importers/notion_markdown.py`
- Modify: `src/writings/importers/planner.py`
- Modify: `src/writings/importers/notion.py`
- Local test only, then delete: `tests/local/test_notion_preview.py`
- Local fixtures only, then delete: `build/notion-import/test-preview/`

**Interfaces:**
- Consumes: Task 1 `open_export`, strict plan contracts, existing `WritingArticle`, `render_article`, `render_article_page`, and supported asset extensions.
- Produces in `writings.catalog` and exports from `writings.__init__`:
  - `validate_writing_bundle(bundle_root: str | Path) -> WritingArticle`; validate exactly one non-link bundle directory and non-link `index.md` using existing front matter rules without weakening `discover_writings` or publisher root checks.
- Extends `models.py`:
  - `CandidateStatus = Literal["ready", "unchanged", "conflict", "blocked", "ignored", "applied"]`.
  - `ConvertedBundle(bundle_root: Path, issues: tuple[ImportIssue, ...])`.
  - `ImportCandidateResult(source_ref: str, slug: str | None, status: CandidateStatus, issues: tuple[ImportIssue, ...], bundle_root: Path | None, source_fingerprint: str | None, written_fingerprint: str | None)`.
  - `ImportRunResult(candidates: tuple[ImportCandidateResult, ...])` with `counts(self) -> Mapping[CandidateStatus, int]` returning every status in fixed order, including zero counts.
- Produces in `notion_markdown.py`:
  - `convert_notion_page(plan_article: ImportArticlePlan, inventory: ExportInventory, selected_routes: Mapping[str, str], destination_root: str | Path) -> ConvertedBundle`; returns the staged bundle plus non-blocking safe warnings, or raises `NotionImportError` with a candidate-safe issue code/message.
- Produces in `planner.py`:
  - `preview_import(inventory: ExportInventory, plan: ImportPlan, preview_root: str | Path, report_path: str | Path) -> ImportRunResult`.
  - `serialize_import_report(result: ImportRunResult) -> str`.
- Extends CLI with `preview EXPORT PLAN`.

- [ ] **Step 1: Write strict single-bundle validation RED tests**

Create local tests:

```python
class SingleBundleValidationTests(unittest.TestCase):
    def test_validates_a_prepared_bundle_without_public_source_root(self):
        bundle = write_bundle(self.root / "alpha", valid_notion_markdown(slug="alpha"))
        article = validate_writing_bundle(bundle)
        self.assertEqual((article.slug, article.source), ("alpha", "notion"))

    def test_rejects_linked_bundle_and_linked_index(self):
        for bundle in (make_linked_bundle(self.root), make_linked_index_bundle(self.root)):
            with self.subTest(bundle=bundle.name), self.assertRaises(WritingCatalogError):
                validate_writing_bundle(bundle)

    def test_matches_discover_writings_front_matter_errors(self):
        bundle = write_bundle(self.root / "alpha", invalid_notion_markdown_without_summary())
        with self.assertRaisesRegex(WritingCatalogError, "summary"):
            validate_writing_bundle(bundle)
        result = discover_writings(self.root, WritingManifest.empty(date(2026, 9, 1)))
        self.assertEqual([(issue.code, issue.source) for issue in result.issues], [("missing_field", "alpha/index.md")])

    def test_does_not_relax_canonical_publisher_source_root(self):
        external = self.root / "external-writings"
        external.mkdir()
        with self.assertRaises(WritingPublishError):
            prepare_writings_publication(external, PROJECT_ROOT / "docs/writings", self.report, date(2026, 9, 1))
```

Use a complete `source: notion` front matter fixture. Assert the returned `WritingArticle`, fixed safe exceptions, and unchanged publisher rejection for a noncanonical public source root.

- [ ] **Step 2: Run validation tests and verify RED**

Run:

```powershell
python -m unittest -v tests.local.test_notion_preview.SingleBundleValidationTests
```

Expected: import error because `validate_writing_bundle` is not public.

- [ ] **Step 3: Expose one reusable strict bundle validator**

Refactor only the already-tested entry/index/front-matter path from `discover_writings` into `validate_writing_bundle`. Preserve existing issue codes and discovery behavior; do not accept arbitrary source roots for publication and do not move renderer/asset validation into catalog.

- [ ] **Step 4: Run validation tests and existing writings smoke probes**

Run the Step 2 command plus a local probe that calls `discover_writings` on one valid, one missing-index, and one linked bundle. Expected: validation tests pass and discovery results match the pre-refactor codes.

- [ ] **Step 5: Write Markdown conversion RED tests**

Add:

```python
class NotionMarkdownTests(unittest.TestCase):
    def test_removes_only_matching_detected_leading_h1(self):
        converted = self.convert("# Source title\n\nBody", detected_title="Source title", title="Public title")
        self.assertNotIn("# Source title", read_index(converted.bundle_root))
        remaining = self.convert("# Different title\n\nBody", detected_title="Source title")
        article = validate_writing_bundle(remaining.bundle_root)
        with self.assertRaises(WritingRenderError) as caught:
            render_article(article, output_file=self.output_file, output_root=self.output_root)
        self.assertEqual(caught.exception.code, "body_h1")

    def test_preserves_code_fences_and_inline_code_during_rewrite(self):
        body = "```md\n![x](private.png)\n```\n\n`[x](private.md)`"
        self.assertIn(body, read_index(self.convert(body).bundle_root))

    def test_converts_callout_aside_to_blockquote_and_warns_on_lossy_html(self):
        converted = self.convert("<aside>Remember **this**</aside>\n<section>lost</section>")
        text = read_index(converted.bundle_root)
        self.assertIn("> Remember **this**", text)
        self.assertIn("invalid_notion_html", [issue.code for issue in converted.issues])

    def test_rewrites_selected_internal_links_and_safe_fragments(self):
        converted = self.convert("[Beta](Beta.md#Part)", routes={"Notes/Beta.md": "beta"})
        self.assertIn("[Beta](beta.html#Part)", read_index(converted.bundle_root))

    def test_blocks_unselected_ambiguous_escaping_and_attachment_links(self):
        for target in ("Beta.md", "../Outside.md", "duplicate.md", "paper.pdf"):
            with self.subTest(target=target), self.assertRaises(NotionImportError):
                self.convert(f"[blocked]({target})", routes={})

    def test_copies_local_images_with_dedup_and_deterministic_collision_names(self):
        converted = self.convert("![a](a/plot.png)\n![b](b/plot.png)\n![same](a/copy.png)")
        assets = sorted(path.name for path in (converted.bundle_root / "assets").iterdir())
        suffix = hashlib.sha256(self.other_image_bytes).hexdigest()[:8]
        self.assertEqual(assets, [f"plot-{suffix}.png", "plot.png"])
        self.assertEqual(read_index(converted.bundle_root).count("assets/plot.png"), 2)

    def test_blocks_remote_missing_escaping_linked_and_unsupported_images(self):
        for target in ("https://example.com/a.png", "missing.png", "../escape.png", "linked.png", "movie.mp4"):
            with self.subTest(target=target), self.assertRaises(NotionImportError):
                self.convert(f"![blocked]({target})")

    def test_emits_canonical_notion_front_matter_and_lf_body(self):
        text = read_index(self.convert("Body\r\nline").bundle_root)
        self.assertTrue(text.startswith("---\ntitle: Public title\nslug: alpha\npublished_at: 2026-09-01\nkind: learning-note\npublic: true\n"))
        self.assertIn("\nsource: notion\n---\n", text)
        self.assertNotIn("\r", text)
```

Assert that selected page links become `<slug>.html`, ordinary `https`/`mailto` links remain, asset destinations are only `assets/<safe-name>`, identical bytes deduplicate, different same-name bytes receive an eight-hex content suffix, and Notion/absolute identifiers never appear in generated front matter.

- [ ] **Step 6: Run conversion tests and verify RED**

Run:

```powershell
python -m unittest -v tests.local.test_notion_preview.NotionMarkdownTests
```

Expected: failures because `convert_notion_page` does not exist.

- [ ] **Step 7: Implement deterministic Markdown, HTML, link, and asset conversion**

Protect fenced code and inline code before parsing links/images/HTML. Decode UTF-8 with optional BOM, normalize LF, remove only the matching detected leading H1, and leave every other H1 for strict renderer rejection. Parse recognized `<aside>...</aside>` blocks with `HTMLParser`, reject unsafe nested markup, and emit blockquote text; record `invalid_notion_html` when unrecognized HTML may lose content.

Resolve local references against the source page's parent using the inventory mapping, not the host filesystem. Decode once, normalize POSIX components, preserve a validated fragment, and reject unselected/ambiguous/escaping/local-attachment targets. Sanitize image basenames, copy bytes from inventory files, and use content hashes for collisions/deduplication.

Serialize YAML in the V2 canonical order: `title`, `slug`, `published_at`, `kind`, `public`, `summary`, `tags`, `source`. Set `public: true`, `source: notion`, and never add unknown fields.

- [ ] **Step 8: Run conversion tests and verify GREEN**

Run the Step 6 command. Expected: every conversion/security case passes.

- [ ] **Step 9: Write preview orchestration RED tests**

Add:

```python
class PreviewTests(unittest.TestCase):
    def test_requires_exact_export_fingerprint(self):
        with self.assertRaisesRegex(NotionImportError, "export fingerprint"):
            preview_import(self.inventory, replace(self.plan, export_fingerprint="sha256:" + "0" * 64), self.preview, self.report)

    def test_ignored_and_blocked_candidates_do_not_stop_ready_candidate(self):
        result = preview_import(self.inventory, self.mixed_plan, self.preview, self.report)
        self.assertEqual({item.slug: item.status for item in result.candidates}, {"alpha": "ready", "broken": "blocked", "ignored": "ignored"})

    def test_ready_candidate_passes_catalog_renderer_and_writes_local_html(self):
        result = preview_import(self.inventory, self.ready_plan, self.preview, self.report)
        self.assertEqual(result.candidates[0].status, "ready")
        html = (self.preview / "site/writings/alpha.html").read_text(encoding="utf-8")
        self.assertIn("<h1>Alpha</h1>", html)

    def test_report_redacts_ids_paths_and_bodies(self):
        preview_import(self.inventory, self.mixed_plan, self.preview, self.report)
        report = self.report.read_text(encoding="utf-8")
        self.assertNotIn("0123456789abcdef0123456789abcdef", report)
        self.assertNotIn(str(self.export_root.resolve()), report)
        self.assertNotIn("PRIVATE BODY SENTINEL", report)

    def test_preview_rebuild_removes_stale_preview_only_inside_build(self):
        stale = self.preview / "stale.html"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        preview_import(self.inventory, self.ready_plan, self.preview, self.report)
        self.assertFalse(stale.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_cli_preview_returns_zero_with_blocked_candidates_and_two_on_global_error(self):
        self.assertEqual(run_cli("preview", self.export, self.plan_path)[0], 0)
        self.assertEqual(run_cli("preview", self.export, self.invalid_plan_path)[0], 2)
```

The mixed fixture has one valid selected page, one selected page with an unresolved link, and one ignored page. Assert statuses `ready`, `blocked`, `ignored`, a complete local HTML preview, copied local image, safe report counts, and no `content/` or `docs/` mutation.

- [ ] **Step 10: Run preview tests and verify RED**

Run:

```powershell
python -m unittest -v tests.local.test_notion_preview.PreviewTests
```

Expected: failures for missing preview/result/report orchestration.

- [ ] **Step 11: Implement preview, report, and CLI integration**

Validate the plan and exact export fingerprint before clearing or writing preview output. For each selected candidate, rebuild a staged bundle, call `validate_writing_bundle`, call `render_article`, copy only returned asset instructions into preview site paths, and wrap with `render_article_page`. Convert known validation/render errors to safe candidate issues and continue. Global root/fingerprint/plan errors stop before preview mutation.

Reports use version `1`, deterministic status counts, redacted source refs, public slugs, and `{code, message}` only. CLI success prints one line of counts and the relative preview/report paths; degraded preview still returns `0`, global invalid input returns `2`.

- [ ] **Step 12: Run all Task 2 verification**

Run:

```powershell
python -m unittest -v tests.local.test_notion_preview
python -m compileall -q src
python -m writings.importers.notion preview build/notion-import/test-preview/export.zip build/notion-import/test-preview/plan.yaml
git diff --check
```

Open the local preview and verify at 390, 768, and 1440 pixels in light/dark modes: no page overflow, drawer/collapse works, article TOC works, code/table/image containers scroll locally, and sanitized callout/body typography matches V2.

- [ ] **Step 13: Remove local artifacts and commit Task 2**

Delete the Task 2 test module/package/cache, fixtures, preview, reports, and browser captures. Verify no fake bundle exists below `content/writings/`. Stage only production source, run cached diff checks, inspect staged paths, and commit:

```powershell
git commit -m "feat: preview notion pages as writings"
```

---

### Task 3: Private state, guarded apply, and end-to-end import

**Files:**
- Modify: `src/writings/importers/models.py`
- Create: `src/writings/importers/state.py`
- Create: `src/writings/importers/promoter.py`
- Modify: `src/writings/importers/planner.py`
- Modify: `src/writings/importers/notion.py`
- Local test only, then delete: `tests/local/test_notion_apply.py`
- Local fixtures only, then delete: `build/notion-import/test-apply/`
- Generated locally for verification, then restore/delete: `content/writings/<fixture-slug>/`, `docs/writings/`, `docs/search-index.json`, `build/reports/`

**Interfaces:**
- Consumes: Tasks 1–2 inventory, plan, conversion, validation, preview status, and existing unified `papers.site.generate_site`.
- Extends `models.py`:
  - `ImportStateEntry(source_key: str, slug: str, source_fingerprint: str, written_fingerprint: str)`.
  - `ImportState(version: int, sources: Mapping[str, ImportStateEntry])` with immutable mapping.
- Produces in `state.py`:
  - `load_import_state(path: str | Path) -> ImportState`.
  - `serialize_import_state(state: ImportState) -> str`.
  - `write_import_state(path: str | Path, state: ImportState) -> None` using verified atomic sibling replacement.
  - `source_key(source_ref: str) -> str`; prefer a recognized final 32-hex filename identifier internally, otherwise return a SHA-256 key of normalized relative source ref. Never expose the raw key in diagnostics.
  - `fingerprint_bundle(bundle_root: str | Path) -> str`; hash sorted normalized relative paths plus file bytes and reject links/special files.
- Produces in `promoter.py`:
  - `apply_import(inventory: ExportInventory, plan: ImportPlan, content_root: str | Path, state_path: str | Path, work_root: str | Path, report_path: str | Path) -> ImportRunResult`.
- Extends `planner.inspect_export` to `inspect_export(inventory: ExportInventory, previous: ImportPlan | None = None, state: ImportState | None = None) -> ImportPlan`; when a strong private source key matches state and exactly one previous-plan article, preserve its reviewed metadata and state-owned slug across a renamed export path. Ambiguous matches raise `ambiguous_identity` rather than guessing.
- Extends CLI with `apply EXPORT PLAN` and fixed canonical defaults: content root `content/writings`, state `build/notion-import/state.json`, work root `build/notion-import`, report `build/reports/notion-import.json`.

- [ ] **Step 1: Write state and fingerprint RED tests**

Create:

```python
class ImportStateTests(unittest.TestCase):
    def test_state_round_trip_is_deterministic_and_private(self):
        text = serialize_import_state(self.state)
        self.assertEqual(text, serialize_import_state(load_state_text(self.root, text)))
        self.assertNotIn("PRIVATE BODY", text)
        self.assertNotIn(str(self.root.resolve()), text)

    def test_state_rejects_unknown_fields_bad_hashes_duplicate_slugs_and_absolute_values(self):
        for payload in invalid_state_payloads():
            with self.subTest(case=payload["case"]), self.assertRaises(NotionImportError):
                load_import_state(write_state_payload(self.root, payload["value"]))

    def test_source_key_prefers_filename_id_but_never_leaks_it_in_messages(self):
        page_id = "0123456789abcdef0123456789abcdef"
        key = source_key(f"Notes/Alpha {page_id}.md")
        self.assertEqual(key, "notion:" + page_id)
        self.assertNotIn(page_id, redact_source_ref(f"Notes/Alpha {page_id}.md"))

    def test_bundle_fingerprint_is_stable_and_changes_for_path_or_bytes(self):
        first = fingerprint_bundle(self.bundle)
        self.assertEqual(first, fingerprint_bundle(self.bundle))
        (self.bundle / "index.md").write_text("changed", encoding="utf-8")
        self.assertNotEqual(first, fingerprint_bundle(self.bundle))

    def test_bundle_fingerprint_rejects_links_junctions_and_special_files(self):
        for bundle in (linked_bundle(self.root), junction_bundle(self.root), special_file_bundle(self.root)):
            with self.subTest(bundle=bundle.name), self.assertRaises(NotionImportError):
                fingerprint_bundle(bundle)

    def test_state_write_is_atomic_and_stays_below_build(self):
        write_import_state(self.state_path, self.state)
        self.assertEqual(load_import_state(self.state_path), self.state)
        with self.assertRaises(NotionImportError):
            write_import_state(self.root / "outside.json", self.state)
        self.assertFalse(list(self.state_path.parent.glob(".notion-state-*")))
```

Assert strict `sha256:<64 lowercase hex>` values, one source per slug, stable JSON ordering, no body bytes, and no raw IDs/absolute paths in raised errors.

- [ ] **Step 2: Run state tests and verify RED**

Run:

```powershell
python -m unittest -v tests.local.test_notion_apply.ImportStateTests
```

Expected: import failure because `state.py` and state models do not exist.

- [ ] **Step 3: Implement strict private state and bundle fingerprints**

Load only version `1` with exact fields. Resolve state path lexically and physically below canonical project `build/notion-import/`; reject links/junctions in parent chain. State writes use a sibling temporary file with flush/fsync where supported and `os.replace`. Fingerprints include each relative POSIX path, separator, byte length, and file SHA-256 in sorted order.

- [ ] **Step 4: Run state tests and verify GREEN**

Run Step 2. Expected: all state and fingerprint tests pass.

- [ ] **Step 5: Write guarded promotion RED tests**

Add:

```python
class ApplyImportTests(unittest.TestCase):
    def test_first_apply_creates_bundle_and_state(self):
        result = self.apply(self.ready_export, self.ready_plan)
        self.assertEqual(result.candidates[0].status, "applied")
        self.assertTrue((self.content / "alpha/index.md").is_file())
        self.assertEqual(load_import_state(self.state_path).sources[source_key(self.alpha_ref)].slug, "alpha")

    def test_unchanged_reapply_is_noop(self):
        self.apply(self.ready_export, self.ready_plan)
        before = tree_bytes(self.content / "alpha")
        result = self.apply(self.ready_export, self.ready_plan)
        self.assertEqual(result.candidates[0].status, "unchanged")
        self.assertEqual(tree_bytes(self.content / "alpha"), before)

    def test_changed_export_updates_only_state_trusted_unedited_bundle(self):
        self.apply(self.ready_export, self.ready_plan)
        changed = export_with_alpha_body("updated")
        result = self.apply(changed, inspect_and_review(changed, previous=self.ready_plan))
        self.assertEqual(result.candidates[0].status, "applied")
        self.assertIn("updated", (self.content / "alpha/index.md").read_text(encoding="utf-8"))

    def test_missing_state_or_occupied_slug_is_conflict(self):
        write_bundle(self.content / "alpha", "human")
        result = self.apply(self.ready_export, self.ready_plan)
        self.assertEqual(result.candidates[0].status, "conflict")
        self.assertEqual((self.content / "alpha/index.md").read_text(encoding="utf-8"), "human")

    def test_human_edit_is_conflict_without_force_escape_hatch(self):
        self.apply(self.ready_export, self.ready_plan)
        (self.content / "alpha/index.md").write_text("human edit", encoding="utf-8")
        result = self.apply(export_with_alpha_body("notion edit"), self.updated_plan)
        self.assertEqual(result.candidates[0].status, "conflict")
        self.assertNotIn("force", parser_option_names(build_parser()))

    def test_title_change_reuses_state_slug(self):
        self.apply(self.ready_export, self.ready_plan)
        renamed = export_with_renamed_alpha_title()
        plan = inspect_and_review(renamed, previous=self.ready_plan, state=load_import_state(self.state_path))
        self.assertEqual(plan.articles[0].slug, "alpha")

    def test_invalid_candidate_does_not_block_valid_candidate(self):
        result = self.apply(self.mixed_export, self.mixed_plan)
        self.assertEqual({item.slug: item.status for item in result.candidates}, {"alpha": "applied", "broken": "blocked"})

    def test_bundle_swap_rolls_back_when_state_write_fails(self):
        self.apply(self.ready_export, self.ready_plan)
        before = tree_bytes(self.content / "alpha")
        with inject_state_write_failure():
            result = self.apply(export_with_alpha_body("update"), self.updated_plan)
        self.assertEqual(result.candidates[0].status, "blocked")
        self.assertEqual(tree_bytes(self.content / "alpha"), before)

    def test_failed_rollback_and_ambiguous_residue_are_global_fatal(self):
        make_ambiguous_backup(self.content, "alpha")
        with self.assertRaisesRegex(NotionImportError, "recovery"):
            self.apply(self.ready_export, self.ready_plan)

    def test_apply_never_touches_docs_or_unmanaged_content(self):
        before_docs = tree_bytes(self.docs)
        unmanaged = self.content / "manual.txt"
        unmanaged.write_text("keep", encoding="utf-8")
        self.apply(self.ready_export, self.ready_plan)
        self.assertEqual(tree_bytes(self.docs), before_docs)
        self.assertEqual(unmanaged.read_text(encoding="utf-8"), "keep")

    def test_cli_apply_reports_counts_and_safe_errors(self):
        code, stdout, stderr = run_cli("apply", self.ready_export, self.ready_plan_path)
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("applied=1", stdout)
        self.assertNotIn("0123456789abcdef0123456789abcdef", stdout)
```

Capture file hashes before and after every failure. Assert no `--force` parser option, exact statuses, per-article isolation, state/bundle agreement after success, byte-for-byte rollback after injected state failure, and global stop before subsequent candidates when rollback cannot be proven.

Unit tests patch the module's immutable `PROJECT_ROOT` seam to an isolated project-shaped tree containing `content/writings/` and `build/notion-import/`; production calls expose no project-root override. A separate end-to-end test uses the real canonical roots with uniquely prefixed fixture slugs and mandatory `finally` cleanup.

- [ ] **Step 6: Run apply tests and verify RED**

Run:

```powershell
python -m unittest -v tests.local.test_notion_apply.ApplyImportTests
```

Expected: failures because guarded promotion/apply are absent.

- [ ] **Step 7: Implement per-article atomic apply and recovery guards**

Define one module-level `PROJECT_ROOT` from the importer package location and validate canonical project roots lexically and after resolution. Do not expose a CLI or public-function project-root override. Rebuild candidates from inventory/plan, never copy preview output. New slugs require a missing target. Existing slugs require a unique state mapping and current fingerprint equal to `written_fingerprint`. Return `unchanged` when candidate and current fingerprints match.

For an update, stage a complete sibling bundle inside `content/writings/`, rename the current target to a uniquely named backup, promote the staged directory, then atomically persist next state. If state persistence fails, remove only the promoted target and restore the verified backup. Remove backup only after state success. Detect leftover importer-prefixed stage/backup directories before mutation; if ownership/recovery is ambiguous, raise a global error without deleting them.

Update the report after each proven candidate result using safe atomic report writes. CLI returns `0` for applied/unchanged/conflict/blocked mixtures with a final summary, `2` for global invalid input/state/root, and `3` for failed rollback requiring human intervention.

- [ ] **Step 8: Run apply tests and verify GREEN**

Run Step 6. Expected: all promotion, conflict, rollback, residue, privacy, and CLI tests pass.

- [ ] **Step 9: Write and run end-to-end RED/GREEN verification**

Add a local end-to-end test that:

1. Builds a ZIP with one selected Notion page, one local PNG, an H2/H3 TOC, code, and a safe internal link to a second selected page.
2. Runs inspect, edits the ignored plan with explicit metadata, runs preview, and applies both bundles.
3. Runs `generate_site` with a fixed date against the real canonical project paths after backing up current managed generated files in a verified local temporary directory.
4. Asserts two `source: notion` bundles, two article pages, copied assets, two manifest records, and two `article:<slug>` title-search documents while all paper/model document IDs remain identical.
5. Restores generated files and deletes fixture bundles in `finally`, then asserts `git diff --exit-code` and no staging/backup residue.

Run:

```powershell
python -m unittest -v tests.local.test_notion_apply
python -m compileall -q src
git diff --check
```

Expected: the initial run before Step 7 fails in apply; after implementation the complete suite passes, generated site assertions pass, and repository tracked files return byte-for-byte to HEAD.

- [ ] **Step 10: Perform browser and CLI acceptance checks**

Using only ignored fixtures, run inspect → edit plan → preview → apply → site generation. Verify the preview and final article pages at 390, 768, and 1440 pixels in light/dark modes; confirm search finds both titles and no horizontal page overflow occurs. Then hand-edit one imported bundle and rerun apply; confirm `conflict` and unchanged bytes.

Verify invalid candidates skip while the valid candidate applies, reports redact 32-hex IDs, and `git status --short --ignored` shows only expected ignored artifacts before cleanup.

- [ ] **Step 11: Remove every local artifact and commit Task 3**

Delete local tests/package/cache, ZIP/directory fixtures, plans, state, preview, reports, importer stage/backup residue, fixture bundles, screenshots, and generated verification output. Restore only files created/changed by the local verification after comparing them with the saved HEAD hashes. Confirm:

```powershell
git diff --check
git status --short
git diff -- README.md
```

Stage only Task 3 production source. Run `git diff --cached --check`, inspect staged names and diff, and commit:

```powershell
git commit -m "feat: apply notion imports safely"
```

- [ ] **Step 12: Final branch verification and next-slice inventory**

Recreate local tests only long enough to run the complete Task 1–3 matrix, then delete them again. Run compile, inspect/preview/apply acceptance, deterministic site generation, search/manifest counts, safe report checks, browser matrix, `git diff --check`, and clean-status checks. Confirm README and all generated/local-only artifacts are absent from the branch diff.

Request full-branch review against the spec and this plan. Fix all Critical/Important findings in one bounded final wave, run one scoped re-review, then use `superpowers:verification-before-completion` and `superpowers:finishing-a-development-branch`. After merging/pushing V3, inventory the remaining approved roadmap (WeChat Reading + WSL summarization and `跑得还快` map) and begin the smallest dependency-resolvable design slice automatically.
