# WeChat Reading Summary Import V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private, offline WeChat Reading Markdown workflow that generates reviewable summaries through a loopback WSL model and safely applies the exact reviewed `book-note` bundles.

**Architecture:** Extract a narrow source-neutral apply contract around the proven V3 transaction engine, preserving the Notion wrapper and behavior. Add a self-contained `weread` adapter for Markdown normalization, deterministic planning, loopback structured generation, content-addressed caching, private preview, and explicit apply. Existing writings validation/publishing remains the only path from public bundles to the generated site.

**Tech Stack:** Python 3.12, standard library (`argparse`, `dataclasses`, `hashlib`, `http.client`/`urllib`, `json`, `zipfile`), PyYAML, existing writings renderer/publisher, static HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-01-weread-summary-import-design.md`

## Global Constraints

- Run `git pull --ff-only` successfully before the first project-file modification; never force, reset, rebase, or overwrite after a failed pull.
- Do not modify `README.md`.
- Add directory rules before adding implementation files in a new directory.
- Keep all tests, fixtures, mock services, previews, reports, caches, generated sample articles, and Python caches local and ignored; delete them before every commit.
- Commit production changes by independently reviewable feature with concise English messages and `git diff --cached --check`.
- No WeChat login, cookie, Gateway, cloud model endpoint, browser-side model call, scheduled model call, or CI model call.
- Only literal loopback HTTP model hosts are accepted: `127.0.0.1`, `localhost`, or `::1`; redirects and proxies are disabled.
- Inspect never calls a model or mutates public content; preview may call the model and writes only private output; apply never calls the model and may modify only canonical `content/writings/` plus private state/report paths.
- A candidate failure is isolated and recorded; an unsafe archive, plan, lock, WAL, state, or recovery condition stops globally before unproven mutation.
- Public output contains synthesized notes only, never raw highlights, raw export identifiers, model transcripts, local paths, or fabricated books.
- Use `PYTHONPATH=<worktree>/src` for every Python command in the linked worktree.

---

### Task 1: Source-neutral importer safety and transaction contract

**Files:**

- Modify: `AGENTS.md`
- Modify: `src/writings/importers/models.py`
- Modify: `src/writings/importers/archive.py`
- Modify: `src/writings/importers/state.py`
- Modify: `src/writings/importers/planner.py`
- Modify: `src/writings/importers/promoter.py`
- Modify: `src/writings/importers/notion.py`
- Modify: `src/writings/importers/__init__.py`
- Local test only, then delete: `tests/local/test_importer_core_v4.py`
- Local regression tests only, then delete: reconstructed final V3 Notion suites

**Interfaces:**

- Consumes: existing `ImportRunResult`, `ImportCandidateResult`, bundle fingerprints, durable lock/WAL/recovery implementation, and Notion `prepare_import_candidates()`.
- Produces in `models.py`:

```python
class WritingImportError(ValueError):
    code: str
    message: str

class NotionImportError(WritingImportError):
    pass

class WeReadImportError(WritingImportError):
    pass

@dataclass(frozen=True, slots=True)
class ImportNamespace:
    name: Literal["notion-import", "weread-import"]
    report_name: Literal["notion-import.json", "weread-import.json"]

@dataclass(frozen=True, slots=True)
class PreparedApplyContract:
    namespace: ImportNamespace
    export_fingerprint: str
    source_refs: tuple[str, ...]
    prepare: Callable[[Path, Path], ImportRunResult]
```

- Produces in `models.py`: `canonical_private_root(namespace: ImportNamespace) -> Path` and `private_import_path(value, namespace=NOTION_NAMESPACE, *, exact_root=False) -> Path`, with `NOTION_NAMESPACE` preserving all existing call behavior.
- Produces in `promoter.py`: `apply_prepared_import(contract, content_root, state_path, work_root, report_path) -> ImportRunResult`.
- Preserves: `apply_import(...)`, `canonical_import_root()`, Notion CLI arguments, Notion exception messages/codes, plan schema version, state schema version, WAL schema, fingerprints, reports, and all V3 recovery behavior.

- [ ] **Step 1: Extend project rules before adding the new adapter directory**

Append exact ownership rules to root `AGENTS.md`:

```markdown
## WeChat Reading 导入器约定

- `src/writings/importers/weread/` 只负责本地微信读书 Markdown 归一化、loopback 模型调用、私有缓存、预览与 CLI 编排。
- 可复用的路径安全、状态和事务逻辑保留在 `src/writings/importers/`；Notion 与 WeChat adapter 不得相互依赖。
- 微信读书计划、原始归一化内容、提示词、模型响应、缓存、预览、状态和报告只放在已忽略的 `build/weread-import/` 或 `build/reports/`。
- 只有显式 apply 可以修改 `content/writings/<slug>/`；adapter 不得直接生成 `docs/`。
```

- [ ] **Step 2: Write failing source-neutral core tests**

Create ignored `tests/local/test_importer_core_v4.py`. Include tests that prove:

Name the cases `test_notion_default_namespace_paths_remain_byte_compatible`, `test_weread_namespace_accepts_only_its_canonical_private_root`, `test_cross_namespace_state_report_and_wal_paths_are_rejected`, `test_generic_candidate_source_fingerprint_does_not_depend_on_archive_lookup`, `test_notion_apply_wrapper_matches_generic_apply_result_and_files`, `test_generic_apply_continues_after_one_independent_candidate_failure`, and `test_generic_apply_preserves_scc_atomicity`. Each test must construct an isolated project-shaped tree, compare exact returned objects and file bytes, and assert the named safety failure before mutation.

Reconstruct the final 110-method V3 ignored regression matrix. Run the new focused tests first and verify they fail because `ImportNamespace`, `PreparedApplyContract`, and `apply_prepared_import` do not exist.

- [ ] **Step 3: Introduce generic errors and namespace-safe private paths**

Make `NotionImportError` a compatibility subclass of `WritingImportError`. The base constructor must accept a sanitizer callback but store only a stable safe message. Add `WeReadImportError` whose sanitizer removes book IDs, absolute paths, source filenames, and control characters.

Parameterize private-root validation by the closed `ImportNamespace` value. Keep this compatibility wrapper exact:

```python
def canonical_import_root() -> Path:
    return canonical_private_root(NOTION_NAMESPACE)

def private_import_path(
    value: str | Path,
    namespace: ImportNamespace = NOTION_NAMESPACE,
    *,
    exact_root: bool = False,
) -> Path:
    return _validated_private_path(value, namespace, exact_root=exact_root)
```

Reject namespace names/report names outside the two literal contracts, links/reparse points in any existing component, lexical or resolved escape, and cross-namespace paths.

- [ ] **Step 4: Make archive/state primitives accept an explicit namespace without changing Notion defaults**

Change `open_export(..., namespace=NOTION_NAMESPACE)` and every owned run path to use that namespace. Parameterize state path validation the same way. Preserve V1 file-only export/bundle fingerprint byte contracts and the existing default arguments so every Notion caller remains source-compatible.

- [ ] **Step 5: Extract the generic prepared-apply boundary**

Move only adapter-specific work out of the transaction body. `apply_prepared_import()` must:

```python
def apply_prepared_import(
    contract: PreparedApplyContract,
    content_root: str | Path,
    state_path: str | Path,
    work_root: str | Path,
    report_path: str | Path,
) -> ImportRunResult:
    paths = validate_exact_namespace_paths(
        contract.namespace, content_root, state_path, work_root, report_path
    )
    unique_source_keys(contract.source_refs)
    return _run_durable_import(contract, paths)
```

Require every ready candidate returned by `contract.prepare()` to carry a non-empty `source_fingerprint` and `written_fingerprint`; verify the bundle fingerprint still equals `written_fingerprint`. Remove the archive lookup from durable preflight. Keep dependency SCC ordering and every recovery record shape unchanged.

Turn the old `apply_import()` into a thin Notion wrapper: validate/serialize its plan, compare export fingerprints, build a callback around `prepare_import_candidates()`, populate candidate source fingerprints from the inventory, create `PreparedApplyContract(NOTION_NAMESPACE, ...)`, and delegate.

- [ ] **Step 6: Run focused and complete regression verification**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -m unittest -q tests.local.test_importer_core_v4
python -m unittest -q <all reconstructed V3 modules>
python -m compileall -q src
```

Expected: new core tests pass; all 110 V3 methods pass with zero skips; two V3 deterministic publisher runs still match; `git diff --check` is empty.

- [ ] **Step 7: Delete local tests and commit the isolated refactor**

Delete every test/fixture/cache/report/preview created for this task. Confirm `git status --short --ignored` has no new residue. Stage only the rule and generic/Notion production files, verify no `README.md`, `tests/`, `build/`, `content/writings/<slug>/`, or `docs/` generated output is staged, then commit:

```bash
git commit -m "refactor: share durable writing imports"
```

---

### Task 2: WeChat Markdown inspection and deterministic plan

**Files:**

- Create: `src/writings/importers/weread/AGENTS.md`
- Create: `src/writings/importers/weread/__init__.py`
- Create: `src/writings/importers/weread/models.py`
- Create: `src/writings/importers/weread/markdown.py`
- Create: `src/writings/importers/weread/planner.py`
- Create: `src/writings/importers/weread/__main__.py`
- Local test only, then delete: `tests/local/test_weread_inspect_v4.py`
- Local fixtures only, then delete: `tests/local/fixtures/weread-v4/`

**Interfaces:**

- Consumes: `open_export(..., namespace=WEREAD_NAMESPACE)`, `ExportInventory`, `WeReadImportError`, portable path/fingerprint helpers, strict YAML primitives.
- Produces in `models.py`: immutable `BookNotes`, `NoteSection`, `WeReadArticlePlan`, and `WeReadPlan` version `1`.
- Produces in `markdown.py`: `parse_book_notes(record: ExportFile) -> BookNotes`.
- Produces in `planner.py`: `inspect_export(inventory) -> WeReadPlan`, `load_plan(path) -> WeReadPlan`, `serialize_plan(plan) -> str`, and `write_plan(path, plan) -> None`.
- Produces in `__main__.py`: `inspect EXPORT --plan PLAN`, while later commands are reserved but return a safe not-implemented parser error until their task lands.

- [ ] **Step 1: Create adapter rules before implementation files**

Create `src/writings/importers/weread/AGENTS.md` with exact file ownership, the closed metadata/section aliases from the spec, the rule that raw notes never leave private memory/build output, the loopback-only boundary, and the prohibition on direct public/docs writes outside the apply orchestrator.

- [ ] **Step 2: Write failing normalization and inspection tests**

Create representative ignored fixtures for the three referenced exporter shapes, plus malformed UTF-8, duplicate book IDs, conflicting title sources, unsupported sections, YAML aliases/anchors/tags, unsafe HTML comments, 10,000 candidates, directory links, portable aliases, and ZIP bombs.

Tests must include:

Name the cases `test_three_exporter_shapes_normalize_to_same_book`, `test_front_matter_title_wins_and_warning_is_redacted`, `test_known_sections_and_chapters_preserve_order`, `test_ids_timestamps_comments_and_unknown_sections_do_not_enter_prompt_input`, `test_invalid_utf8_blocks_only_one_trustworthy_candidate`, `test_duplicate_strong_identity_blocks_both_candidates`, `test_plan_is_deterministic_defaults_to_excluded_and_contains_no_private_data`, `test_directory_and_zip_safety_reuses_v3_limits`, and `test_ten_thousand_candidates_remain_linear`. Assert exact normalized tuples, exact serialized plan bytes, safe error codes, unchanged public/private snapshots on global failure, and a bounded 10,000-candidate operation count.

Run only this module and verify failure because the package is not implemented.

- [ ] **Step 3: Implement strict immutable contracts and Markdown normalization**

Define normalized records with tuples/mapping proxies. Parse YAML through a duplicate-key rejecting safe loader, disallow custom tags/objects, bound the front matter and body, remove BOM, and scan Markdown iteratively. Recognize only the metadata and section aliases listed in the spec. Preserve ordered item text and chapter label privately; strip HTML comments, control characters, WeChat URLs, and empty items before returning.

Do not render Markdown or resolve remote images. Return a candidate-local `WeReadImportError` for decode/metadata failures.

- [ ] **Step 4: Implement deterministic inspect and plan serialization**

Use strong `bookId` identity when unique; otherwise derive a stable private source ref from the content hash. Redact the serialized source ref with a short keyed digest; never serialize the raw identity. Suggest collision-free slugs through the existing ASCII slug policy and reserve every suggestion across the plan.

Use exact plan fields and ordering:

```yaml
version: 1
source: wechat-reading-export
export_fingerprint: sha256:<digest>
books:
  - source_ref: book:<redacted digest>
    detected_title: <safe title>
    detected_author: <safe author or null>
    include: false
    slug: <suggestion>
    title: <safe title>
    published_at: null
    summary: null
    tags: [reading]
```

Reject unknown/missing fields, duplicate keys, invalid dates/slugs/tags, non-`book-note`/non-`wechat-reading` fixed semantics, and paths outside canonical `build/weread-import/`. Use atomic replacement.

- [ ] **Step 5: Implement inspect CLI feedback and exit codes**

The parser exposes exact command/help text, resolves canonical private paths, and prints only counts plus `build/weread-import/...`. Global invalid input returns `2` with one safe next action. Successful inspect returns `0` even when some page candidates are blocked, and reports discovered/blocked counts.

- [ ] **Step 6: Verify, clean, and commit inspection**

Run the focused test module, Task 1 generic core tests, reconstructed V3 regression, compileall, and a real directory/ZIP inspect determinism probe. Delete all local artifacts. Stage only adapter rules and Task 2 production files, then commit:

```bash
git commit -m "feat: inspect wechat reading exports"
```

---

### Task 3: Loopback structured summarization and private cache

**Files:**

- Create: `src/writings/importers/weread/prompts.py`
- Create: `src/writings/importers/weread/client.py`
- Create: `src/writings/importers/weread/cache.py`
- Create: `src/writings/importers/weread/summarizer.py`
- Modify: `src/writings/importers/weread/models.py`
- Local test only, then delete: `tests/local/test_weread_summary_v4.py`
- Local mock service only, then delete: `tests/local/weread_mock_server.py`

**Interfaces:**

- Consumes: normalized `BookNotes`, canonical `WEREAD_NAMESPACE` private paths, durable atomic-write primitives.
- Produces: `SummaryResult(one_sentence, key_ideas, reflections, questions)`, `SummaryCacheKey`, `LoopbackChatClient.complete(messages, *, model, timeout) -> str`, and `summarize_book(book, config, cache, refresh=False) -> SummaryResult`.
- Cache path: `build/weread-import/cache/<first-two>/<sha256>.json`.

- [ ] **Step 1: Write failing transport, schema, cache, chunking, and privacy tests**

Tests must start a real local mock HTTP service on `127.0.0.1` and cover:

Name the cases `test_accepts_literal_loopback_openai_chat_completion`, `test_rejects_https_cloud_lan_userinfo_query_fragment_and_redirect`, `test_disables_environment_proxies`, `test_timeout_oversize_non_utf8_and_malformed_json_are_candidate_errors`, `test_structured_response_rejects_unknown_fields_html_controls_and_bounds`, `test_long_source_highlight_copy_is_rejected`, `test_chunks_only_at_item_boundaries_and_reduces_in_stable_order`, `test_cache_hit_avoids_network_and_corrupt_cache_is_not_trusted`, `test_cache_key_changes_with_source_content_prompt_model_and_contract`, and `test_cache_and_exception_text_leak_no_source_path_or_book_id`. The mock records exact requests and connection counts; tests assert exact JSON fields, bounds, error codes, cache bytes, and absence of seeded private markers.

- [ ] **Step 2: Implement fixed prompts and deterministic chunking**

Set constants `PROMPT_VERSION = "weread-summary-v1"`, `TRANSPORT_VERSION = "openai-chat-v1"`, and `MAX_CHUNK_CHARS = 24_000`. Build map prompts from complete normalized items; reject one item exceeding the bound instead of slicing its text. Reduce map results in original chunk order. Explicitly instruct the model to synthesize, avoid long quotations, use Chinese, return JSON only, and not invent facts absent from notes.

- [ ] **Step 3: Implement a proxy-free, redirect-free loopback client**

Normalize a base ending in `/v1`, construct `/chat/completions`, and accept only literal loopback hosts with no userinfo/query/fragment. Use a custom opener/connection path that ignores proxy environment variables and never follows redirects. Bound connect/read time and read at most 4 MiB plus one byte. Send stable JSON with `model`, `messages`, `temperature: 0`, and `stream: false`. Validate HTTP 200, JSON object shape, one choice, and string message content. Convert transport details into safe `WeReadImportError` codes.

- [ ] **Step 4: Validate structured model output and copyright guard**

Parse a JSON object with exactly `one_sentence`, `key_ideas`, `reflections`, and `questions`. Enforce one-line plain strings, item/array bounds from the spec, uniqueness after Unicode normalization, no HTML/control characters, and no unknown fields. Compare every generated segment against whitespace-normalized highlights; reject a shared run of at least 120 characters. Do not compare against user thoughts because they are user-authored source material, but never copy them automatically outside model synthesis.

- [ ] **Step 5: Implement content-addressed durable cache**

Hash canonical JSON containing source fingerprint, selected normalized content fingerprint, prompt version, model, and transport version. Cache an envelope containing version, key inputs, validated result, and checksum. Validate the entire envelope on read; treat corruption as a cache miss without deleting evidence. Write through `durable_atomic_write` below the verified WeChat private root. `refresh=True` always calls the model and atomically replaces only the matching cache key.

- [ ] **Step 6: Verify, clean, and commit summarization**

Run the focused module with the real mock server, Task 2 inspection tests, generic core/V3 regression, compileall, and a residue/leak scan. Delete the mock, tests, cache, and reports. Stage only the four new production modules plus `models.py`, then commit:

```bash
git commit -m "feat: summarize reading notes locally"
```

---

### Task 4: Private preview, exact apply, CLI, and end-to-end publishing

**Files:**

- Create: `src/writings/importers/weread/rendering.py`
- Create: `src/writings/importers/weread/workflow.py`
- Modify: `src/writings/importers/weread/__main__.py`
- Modify: `src/writings/importers/weread/__init__.py`
- Modify: `src/writings/importers/__init__.py`
- Local test only, then delete: `tests/local/test_weread_workflow_v4.py`
- Local end-to-end fixtures only, then delete: `tests/local/fixtures/weread-e2e-v4/`
- Generated locally, then restore/delete: `content/writings/weread-v4-*`, `docs/writings/`, `docs/search-index.json`, `build/weread-import/`, `build/reports/`

**Interfaces:**

- Consumes: WeRead plan/normalization/summarizer/cache, `validate_writing_bundle`, existing article/page renderer, `PreparedApplyContract`, and `apply_prepared_import`.
- Produces: `preview_import(inventory, plan, model_config, refresh=False) -> ImportRunResult`, `apply_import(inventory, plan) -> ImportRunResult`, deterministic public-bundle rendering, redacted report serialization, and complete CLI commands.

- [ ] **Step 1: Write failing preview/apply/CLI and E2E tests**

Cover exact behavior:

Name the cases `test_preview_skips_failed_book_and_continues_to_later_book`, `test_preview_rebuilds_private_site_and_reports_actionable_safe_status`, `test_public_bundle_contains_only_synthesis_and_strict_front_matter`, `test_apply_uses_exact_previewed_cache_and_never_calls_model`, `test_changed_export_or_cache_blocks_before_public_mutation`, `test_apply_is_idempotent_and_detects_human_edit_and_slug_owner_conflict`, `test_renamed_export_with_same_identity_updates_existing_slug`, `test_one_failed_apply_continues_to_independent_book`, `test_two_process_lock_and_crash_matrix_recover_without_data_loss`, `test_cli_exit_zero_two_three_and_messages_are_redacted`, and `test_real_inspect_preview_apply_reapply_publish_is_deterministic`. Assert exact status sequences, report objects, bundle/search bytes, connection count zero during apply, process exit codes, recovery trees, and seeded privacy markers absent from every public file.

The E2E test must prove search contains the book title, two fixed-date `generate_site()` calls are byte-identical, no raw highlight/private ID appears under the public bundle or `docs/`, and cleanup runs in `finally` even after assertion failure.

- [ ] **Step 2: Render deterministic reviewed bundles**

Build exact front matter fields accepted by `validate_writing_bundle`; set `kind: book-note`, `source: wechat-reading`, `public: true`, and ensure `reading` is the first unique tag. Use the plan summary seed when provided, otherwise the generated `one_sentence`. Render author as escaped plain text and only non-empty body sections in the specified order. Never render raw inputs, external cover URLs, progress, identifiers, model metadata, or cache keys.

Fingerprint the completed private bundle after strict validation and store that reviewed fingerprint in the candidate result.

- [ ] **Step 3: Implement per-book private preview and report**

Validate plan/export identity before resetting preview. For each included book, resolve its normalized source, obtain the exact cache entry or generate it, render/validate the bundle, render an article preview page, and record `ready`; convert known candidate errors to `blocked` and continue. `ignored` candidates make no model call. Rebuild only canonical `build/weread-import/preview/` through an empty owned stage, then atomically replace it.

Write `build/reports/weread-import.json` with only version, redacted source ref, slug, status, code, and safe message. Use one report write after candidate processing rather than quadratic full rewrites.

- [ ] **Step 4: Bind exact cached preview output to generic durable apply**

Apply validates the export and plan again, loads the exact cache envelope, deterministically rebuilds each included bundle without a network client, verifies the result equals the reviewed cache/bundle fingerprint recorded by preview state, and returns a prepared candidate callback. Pass `PreparedApplyContract(WEREAD_NAMESPACE, ...)` into `apply_prepared_import()` with only canonical paths.

Do not create dependencies between books. Keep all human-edit, state ownership, first-import, idempotency, lock, WAL, crash, report, and recovery semantics in the shared core.

- [ ] **Step 5: Complete CLI and feedback**

Implement:

```text
inspect EXPORT --plan PLAN
preview EXPORT PLAN --model MODEL [--base-url URL] [--timeout SECONDS] [--refresh-summary]
apply EXPORT PLAN
```

Environment fallbacks are `TOGOS_WSL_LLM_MODEL` and `TOGOS_WSL_LLM_BASE_URL`; never print their raw values. Preview without a model names the missing setting and tells the user to start/configure the local WSL service. Apply exposes no model flags. Print counts in stable status order plus the repository-relative report location. Return `0`, `2`, or `3` exactly as the spec requires.

- [ ] **Step 6: Run full verification and visual QA**

Run the Task 4 module, Tasks 1–3 modules, reconstructed final V3 110-method regression, compileall, diff checks, and the real fixed-date E2E twice. Serve the private preview over local HTTP and inspect desktop/mobile, light/dark, keyboard focus, long Chinese title, empty optional sections, and blocked-book feedback. Confirm the public site remains visually unchanged after fixture cleanup because no real user book is committed.

- [ ] **Step 7: Delete all local artifacts and commit workflow**

Restore canonical generated `docs/` files if the E2E touched them, then delete every test, fixture, mock, preview, cache, report, sample public bundle, local server output, screenshot, and Python cache. Verify Git shows only intended production source. Stage exact files, run `git diff --cached --check`, inspect name-status, and commit:

```bash
git commit -m "feat: preview and apply reading summaries"
```

---

### Task 5: Release verification, review, merge, and remote integration

**Files:**

- No production file changes expected unless review finds a release blocker.
- Local review ledger only, then delete: `.superpowers/sdd/2026-09-01-weread-summary-import-v4/`
- Local reconstructed tests only, then delete: `tests/local/`

**Interfaces:**

- Consumes: Tasks 1–4 commits and acceptance suite.
- Produces: reviewed, clean V4 feature branch merged to `main`, pushed to `origin/main`, with no local test/private residue.

- [ ] **Step 1: Run a fresh complete acceptance matrix**

Reconstruct the final local suites and run them in one command. Require zero failures and no unexplained skips. Run compileall, `git diff --check`, base-to-head `git diff --check`, a privacy scan, and real inspect-preview-apply-reapply-publish determinism. Record exact counts and commands in the ignored review ledger.

- [ ] **Step 2: Request full branch review and correct blockers**

Review the full base-to-head diff for spec compliance, security, privacy, durability, scale, copyright guard, per-book continuation, and regressions. Fix every Critical/Important finding in bounded correction commits, reconstruct/run the relevant full suite, and request scoped re-review until no Critical/Important remains.

- [ ] **Step 3: Clean and prove branch boundaries**

Delete `.superpowers`, tests, reports, cache, preview, model responses, sample content, generated test site, and Python caches after reporting every ledger `Ruling:` to the user. Require a clean feature worktree and prove the branch diff contains no `README.md`, `tests/`, `build/`, real/private export, fake public book, or unrelated generated output.

- [ ] **Step 4: Integrate under the repository Git policy**

In the main checkout, require a clean status and run `git pull --ff-only`. If it fails, do not force; follow the user's existing ordinary-merge authorization only when the divergence is the known local feature/docs history versus remote automated paper updates. Merge remote updates first, merge the V4 branch with `--no-ff`, rerun compileall and deterministic site generation twice, and require a clean result.

- [ ] **Step 5: Push and continue the roadmap**

Push `main` to `origin`, remove the clean merged worktree and local feature branch, and report the merge commit plus verification evidence. Do not activate the travel map until the user supplies real public city coordinates and photographs; otherwise continue with the smallest remaining dependency-resolvable knowledge workflow.
