# Local Knowledge Workbench V5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide one private local CLI for creating, importing, previewing, applying, inspecting, and building all writing sources.

**Architecture:** Add a thin `writings.workbench` application layer over the existing writing publisher and the Notion/WeChat Reading adapters. Original drafts receive their own ignored workspace and exact-preview promotion contract; adapter behavior stays owned by V3/V4 modules, while status and unified preview consume only redacted private metadata.

**Tech Stack:** Python 3.12, argparse, immutable dataclasses, existing Markdown/MathML renderer, existing durable importer transaction primitives, static HTML, JSON.

**Spec:** `docs/superpowers/specs/2026-09-02-local-knowledge-workbench-design.md`

## Global Constraints

- The only public content source mutation is explicit apply below `content/writings/<slug>/`.
- The existing publisher remains the sole owner of `docs/writings/` and `docs/search-index.json`.
- Workbench drafts, previews, reports, state, locks, journals, and test artifacts remain below ignored `build/` or `tests/`.
- The workbench starts no server and exposes no public management endpoint.
- Existing Notion and WeChat Reading safety, identity, preview, apply, and exit-code behavior must not drift.
- Local paper inference remains HTML-first with PDF fallback only when HTML is unavailable; V5 adds no downloader.
- New tests and fixtures are local-only, are removed before each commit, and are never staged.
- `README.md` must remain byte-for-byte unchanged.

---

### Task 1: Workbench contracts, command shell, and private original drafts

**Files:**
- Create: `src/writings/workbench/AGENTS.md`
- Create: `src/writings/workbench/__init__.py`
- Create: `src/writings/workbench/__main__.py`
- Create: `src/writings/workbench/models.py`
- Create: `src/writings/workbench/paths.py`
- Create: `src/writings/workbench/drafts.py`
- Create: `src/writings/workbench/cli.py`
- Test locally then delete: `tests/local/test_workbench_drafts.py`
- Test locally then delete: `tests/local/test_workbench_cli.py`

**Interfaces:**
- Produces: `WorkbenchError(code: str, message: str)` with safe bounded fields.
- Produces: `SourceName = Literal["original", "notion", "weread"]`.
- Produces: `workbench_root() -> Path`, `draft_root() -> Path`, `preview_root() -> Path`, `report_path() -> Path`, and `state_path() -> Path`; all return canonical approved paths.
- Produces: `create_draft(slug: str, title: str, kind: str, published_at: date) -> Path`.
- Produces: `run(argv: Sequence[str] | None = None) -> int` and `python -m writings.workbench`.

- [ ] **Step 1: Add the directory rule before implementation files**

Write `src/writings/workbench/AGENTS.md` with the ownership and privacy rules from the spec. It must explicitly forbid absolute paths, bodies, source IDs, model responses, credentials, and stack traces in normal feedback or reports.

- [ ] **Step 2: Write failing path and draft tests**

Create ignored local tests that assert:

```python
assert workbench_root() == PROJECT_ROOT / "build" / "writings-workbench"
path = create_draft("rendering-notes", "Rendering Notes", "learning-note", date(2026, 9, 2))
assert path == PROJECT_ROOT / "build" / "writings-workbench" / "drafts" / "rendering-notes"
text = (path / "index.md").read_text(encoding="utf-8")
assert "slug: rendering-notes" in text
assert "kind: learning-note" in text
assert "source: original" in text
assert 'summary: ""' in text
```

Also cover invalid/portable-colliding slugs, symlink/junction/reparse ancestors, an occupied public slug, non-empty existing draft, and no mutation on every refusal.

- [ ] **Step 3: Run the draft tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_drafts -v
```

Expected: import failure because `writings.workbench` does not exist.

- [ ] **Step 4: Implement immutable contracts and canonical paths**

In `models.py`, add frozen slotted dataclasses for `WorkbenchIssue`, `DraftRecord`, `SourceSummary`, `BuildSummary`, and `WorkbenchStatus`. Validate status vocabulary in `__post_init__`; sanitize user-visible text to one line and fixed maximum lengths.

In `paths.py`, resolve only the fixed project-relative tree. Validate every existing component with `os.path.lexists`, `Path.is_symlink()`, `Path.is_junction()` when available, and Windows reparse attributes. Do not accept caller-supplied output roots.

- [ ] **Step 5: Implement draft creation**

`create_draft` must validate `SLUG_PATTERN`, `SUPPORTED_KINDS`, title, and ISO date; compare sibling and public names with `casefold()`; create parents safely; and use `shared.rendering.atomic_write_text` for `index.md`. The template is:

```markdown
---
title: "<YAML-safe title>"
slug: <slug>
published_at: <date>
kind: <kind>
public: true
summary: ""
tags: []
source: original
---

# <title>

在这里开始整理正文。
```

- [ ] **Step 6: Write failing CLI tests**

Test `new`, global help, missing arguments, invalid kind/date, existing draft, and safe messages. Assert exit `0` for creation and `2` for invalid/unsafe input. Assert output uses `build/writings-workbench/...` and contains no absolute workspace path.

- [ ] **Step 7: Run CLI tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_cli -v
```

Expected: failure because `cli.run` and parser commands are absent.

- [ ] **Step 8: Implement the command shell and `new`**

Build one `_SafeParser` with all six top-level commands so later tasks add handlers without changing public syntax. Catch only `WorkbenchError`, established importer safe errors, and expected local `OSError`; return `2` with one remediation. Let unexpected programming errors surface in tests.

- [ ] **Step 9: Run local Task 1 verification**

Run both local modules plus:

```text
python -m writings.workbench --help
python -m compileall -q src
git diff --check
```

Expected: tests pass; help lists `status`, `new`, `import`, `preview`, `apply`, and `build`.

- [ ] **Step 10: Delete tests and commit Task 1**

Delete `tests/local/test_workbench_drafts.py`, `tests/local/test_workbench_cli.py`, generated drafts, reports, previews, and caches. Confirm `git status --ignored --short` has no Task 1 residue requiring cleanup. Stage only the rule and source modules, run `git diff --cached --check`, verify `README.md` is absent, then commit:

```text
git commit -m "feat: add local writing workbench"
```

---

### Task 2: Exact original preview and durable reviewed apply

**Files:**
- Modify: `src/writings/workbench/models.py`
- Modify: `src/writings/workbench/drafts.py`
- Create: `src/writings/workbench/state.py`
- Create: `src/writings/workbench/preview.py`
- Modify: `src/writings/workbench/cli.py`
- Test locally then delete: `tests/local/test_workbench_original_preview.py`
- Test locally then delete: `tests/local/test_workbench_original_apply.py`

**Interfaces:**
- Consumes: `validate_writing_bundle(bundle_root) -> WritingArticle`.
- Consumes: `render_article(article) -> RenderedArticle` and `render_article_page(...) -> str`.
- Consumes: `fingerprint_bundle(path) -> str` from the shared importer state contract.
- Produces: `preview_original(slug: str) -> OriginalResult`.
- Produces: `apply_original(slug: str) -> OriginalResult`.
- Produces: `load_workbench_state(path: Path) -> WorkbenchState` and `serialize_workbench_state(state) -> str`.

- [ ] **Step 1: Write failing exact-preview tests**

Create a valid draft fixture and assert preview:

- validates the current exact bundle;
- renders below `build/writings-workbench/previews/original/<slug>/`;
- copies only referenced local assets;
- records a `sha256:` preview fingerprint and no article body in state/report;
- leaves the prior preview and state unchanged after an invalid edit;
- reports `ready` or `blocked` with safe remediation.

- [ ] **Step 2: Run preview tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_original_preview -v
```

Expected: failure because preview/state functions are missing.

- [ ] **Step 3: Implement strict private state**

State version `1` maps original slugs to exactly:

```json
{
  "preview_fingerprint": "sha256:...",
  "written_fingerprint": null,
  "preview_page": "previews/original/<slug>/index.html"
}
```

Reject unknown fields, unsafe slugs/paths, malformed hashes, duplicate portable names, unsafe state roots, and non-UTF-8/invalid JSON. Write state atomically and never serialize absolute paths.

- [ ] **Step 4: Implement atomic original preview**

Render one draft into a unique sibling staging directory inside the private preview parent. Validate source and destination containment before copying each asset. Flush, rename the old preview to a backup, rename staging into place, atomically update state/report, and restore the old preview if state/report persistence fails. Remove backup only after the committed state is reread and matches.

- [ ] **Step 5: Write failing apply tests**

Cover first apply, unchanged reapply, changed draft without a new preview, previewed update, public human edit conflict, public slug occupied without workbench state, lock contention, injected failures before/after public rename, restart recovery, and unprovable residue.

- [ ] **Step 6: Run apply tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_original_apply -v
```

Expected: failure because `apply_original` is missing.

- [ ] **Step 7: Implement durable reviewed apply**

Use the existing durable boundary rather than a parallel copy algorithm. Define a dedicated original namespace and `PreparedApplyContract` whose `prepare` callback copies the exact reviewed draft into the transaction bundle root and returns one `ready` candidate with source and written fingerprints. Use fixed paths under `build/writings-workbench/` and a workbench-specific report serializer. Extend the shared namespace vocabulary only as needed; preserve Notion and WeChat behavior with regression tests.

Apply must compare the current draft fingerprint to `preview_fingerprint` before entering the transaction. After a successful transaction, project the shared state fingerprint into workbench state. Return `unchanged` when both exact bytes and state already match.

- [ ] **Step 8: Wire `preview original` and `apply original`**

Map `ready`, `applied`, and `unchanged` to exit `0`; `blocked` and `conflict` to exit `3`; unsafe/global failures to exit `2`. Output only slug, status, repository-relative preview/report paths, and one remediation.

- [ ] **Step 9: Run Task 2 and existing importer regression verification**

Run local original tests plus temporary regression tests that call the existing Notion and WeChat prepared-apply paths. Then run:

```text
python -m compileall -q src
git diff --check
```

Expected: original tests pass and both existing namespaces retain their exact paths, state formats, and status projection.

- [ ] **Step 10: Delete tests and commit Task 2**

Delete all local tests and `build/writings-workbench/` runtime data. Remove generated public fixture bundles. Stage only Task 2 source files, inspect the staged diff, run `git diff --cached --check`, and commit:

```text
git commit -m "feat: preview and publish original drafts"
```

---

### Task 3: Adapter orchestration and unified private preview

**Files:**
- Create: `src/writings/workbench/adapters.py`
- Modify: `src/writings/workbench/preview.py`
- Modify: `src/writings/workbench/cli.py`
- Test locally then delete: `tests/local/test_workbench_adapters.py`
- Test locally then delete: `tests/local/test_workbench_preview_index.py`

**Interfaces:**
- Produces: `inspect_adapter(source: Literal["notion", "weread"], export: str) -> int`.
- Produces: `preview_adapter(source, export, *, model, base_url, timeout, refresh) -> int`.
- Produces: `apply_adapter(source, export) -> int`.
- Produces: `rebuild_preview_index() -> Path`.
- Consumes: `writings.importers.notion.run(argv) -> int` and `writings.importers.weread.__main__.run(argv) -> int`.

- [ ] **Step 1: Write failing adapter-dispatch tests**

Patch only adapter entry-point functions and assert the exact forwarded arguments:

```python
assert notion_args == ["inspect", export, "--plan", "build/notion-import/plan.yaml"]
assert weread_args == ["inspect", export, "--plan", "build/weread-import/plan.yaml"]
```

Preview/apply must use the same plans. WeChat preview forwards model/base URL/timeout/refresh only when provided. Assert the adapter exit code is returned unchanged and a failed adapter never invokes another adapter.

- [ ] **Step 2: Run dispatch tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_adapters -v
```

Expected: import failure because `adapters.py` is absent.

- [ ] **Step 3: Implement thin adapter calls**

Use canonical repository-relative plan strings and direct Python function calls, not subprocess command strings. Do not catch or rewrite the adapters' safe terminal feedback. Refuse `original` in import dispatch and refuse model options for Notion.

- [ ] **Step 4: Write failing unified-preview tests**

Create redacted report fixtures and safe private preview pages for each source. Assert the generated index:

- links only to existing pages below approved preview roots;
- shows source/count/status/slug/safe remediation only;
- never includes report fields outside the allowed vocabulary;
- omits malformed/unsafe links and marks that source `attention`;
- is byte-identical for identical evidence;
- preserves the last valid index when staging/write/rename fails.

- [ ] **Step 5: Run preview-index tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_preview_index -v
```

Expected: failure because `rebuild_preview_index` is missing.

- [ ] **Step 6: Implement the unified private index**

Parse only known versioned fields from the workbench, Notion, and WeChat reports. Build standalone escaped HTML with relative links; do not copy adapter page bodies. Write through a same-directory staging file and atomic replace. Rebuild after every preview result that safely produced or retained a source preview, including degraded exit `3`.

- [ ] **Step 7: Wire import/preview/apply commands**

The CLI syntax must match the spec exactly. Preserve adapter exit codes. If adapter preview returns `0` or `3` but unified-index regeneration fails, return `2` while leaving the adapter's valid preview intact and print the index remediation.

- [ ] **Step 8: Run Task 3 verification**

Run both local modules, existing adapter `--help` commands, workbench `--help`, Python compilation, and `git diff --check`. Confirm no invocation performs cloud model traffic and the only model-capable path remains WeChat preview.

- [ ] **Step 9: Delete tests and commit Task 3**

Delete tests, plans, reports, previews, caches, and mock export data. Stage only Task 3 modules, inspect the staged diff, run `git diff --cached --check`, and commit:

```text
git commit -m "feat: unify local writing imports"
```

---

### Task 4: Redacted status, canonical public build, and end-to-end verification

**Files:**
- Create: `src/writings/workbench/status.py`
- Create: `src/writings/workbench/build.py`
- Modify: `src/writings/workbench/cli.py`
- Modify: `src/writings/workbench/models.py`
- Test locally then delete: `tests/local/test_workbench_status.py`
- Test locally then delete: `tests/local/test_workbench_build.py`
- Test locally then delete: `tests/local/test_workbench_end_to_end.py`

**Interfaces:**
- Produces: `collect_status() -> WorkbenchStatus`.
- Produces: `serialize_status(status: WorkbenchStatus) -> str`.
- Produces: `render_status(status: WorkbenchStatus) -> str`.
- Produces: `build_site(*, generated_on: date | None = None) -> BuildSummary`.
- Consumes: `papers.site.generate_site(...)` with canonical project inputs only.

- [ ] **Step 1: Write failing status tests**

Cover absent workspace, incomplete and ready original drafts, conflicts, valid/degraded adapter reports, malformed report JSON, unknown fields, unsafe paths, and a V2 writing report with published/retained/skipped/removed issues. Assert text and JSON have identical counts and no body, source ID, model output, credential, environment value, or absolute path.

- [ ] **Step 2: Run status tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_status -v
```

Expected: failure because status functions are absent.

- [ ] **Step 3: Implement strict status aggregation**

Treat missing evidence as `not-started`. Parse every report with an exact version/field vocabulary; an invalid report creates one safe `attention` issue and does not block other sources. Determine original `draft`/`ready`/`unchanged`/`conflict` from validation plus exact state fingerprints without reading body text into output records.

Serialize deterministic JSON with `ensure_ascii=False`, sorted source rows, stable slug order, and one trailing newline. The text renderer starts with total actionable counts and then prints only rows that need an action.

- [ ] **Step 4: Write failing canonical-build tests**

Patch `generate_site` and assert exact canonical arguments. In an isolated local fixture, assert:

- clean result returns `0`;
- a single new broken article plus one valid article returns degraded `3`, publishes the valid article, and records the broken one;
- a previously published broken update retains its page/search record;
- unsafe global manifest/search failure returns `2` and does not claim a safe result.

- [ ] **Step 5: Run build tests and verify RED**

Run:

```text
python -m unittest tests.local.test_workbench_build -v
```

Expected: failure because `build_site` is absent.

- [ ] **Step 6: Implement the canonical build wrapper**

Call `generate_site` with fixed paths from the spec and no user output overrides. After success, strictly load `build/reports/writings.json` and return a `BuildSummary`. Issues/skipped/retained caused by article failures yield degraded status; clean publication yields success. Catch established global catalog/publish/I/O errors, print one actionable safe message, and return exit `2`.

- [ ] **Step 7: Wire `status` and `build`**

`status --json` writes only JSON to stdout. Default status writes the compact human view. `build` prints published/retained/skipped/removed counts and the report path, then returns `0`, `2`, or `3` according to the spec.

- [ ] **Step 8: Write and run end-to-end local workflow tests**

Use local fixtures to perform:

```text
new -> edit -> preview original -> apply original
import notion -> preview notion -> apply notion
import weread -> preview weread -> apply weread
build -> status -> build again
```

Use a loopback mock for WeChat summary requests and no external network. Assert three title-search records, deterministic second build, private/public path separation, and one broken extra article being skipped without blocking the three valid articles.

- [ ] **Step 9: Run full verification**

Run:

```text
python -m unittest discover -s tests
python -m compileall -q src
node --check docs/assets/js/sidebar.js
node --check docs/assets/js/search.js
node --check docs/assets/js/search-core.js
git diff --check
```

Run the public privacy scan for absolute workspace paths, `build/`, prompts, model responses, private state names, and local service identifiers. Rebuild the real site twice using a fixed date and confirm the second generated tree is unchanged except explicitly restored date metadata.

- [ ] **Step 10: Remove all local artifacts**

Delete every new test/fixture/mock, generated sample draft/article, plan, preview, report, cache, lock, journal, transaction directory, Python cache, and local build residue. Confirm `git status --short --untracked-files=all` contains only intended source changes and generated public files caused by current real repository inputs.

- [ ] **Step 11: Commit Task 4**

Stage only Task 4 source and required deterministic public outputs. Do not stage `tests/`, `build/`, private content, or `README.md`. Run `git diff --cached --check`, inspect `git diff --cached --name-only`, and commit:

```text
git commit -m "feat: build and inspect local knowledge workflow"
```

- [ ] **Step 12: Final branch verification and push**

Rerun compilation, CLI help, full local tests recreated only for this verification and then deleted, JavaScript checks, canonical site build, public privacy scan, `git diff --check`, and clean-worktree inspection. Confirm `README.md` is unchanged from the pre-V5 commit. Push `main` without force only after local `main` still fast-forwards `origin/main`; if remote moved, stop and follow the repository's sync rule.
