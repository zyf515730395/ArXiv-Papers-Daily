# Local Knowledge Workbench V5 Design

## Purpose

V5 turns the existing writing publisher, Notion importer, and WeChat Reading
importer into one local authoring workflow. The public site already presents
learning notes and book notes; V5 removes the need to remember three unrelated
command sets before real content can be published.

The user-facing entry point is:

```text
python -m writings.workbench status
python -m writings.workbench new ...
python -m writings.workbench import ...
python -m writings.workbench preview ...
python -m writings.workbench apply ...
python -m writings.workbench build
```

The workbench runs only on the local machine. It is not copied to `docs/`, does
not start a network listener, and is not part of the future public server.

## Approved scope

V5 includes:

- one CLI for original drafts, Notion exports, and WeChat Reading exports;
- a private original-draft area below ignored `build/writings-workbench/`;
- safe templates for new `learning-note` and `book-note` drafts;
- adapter dispatch that reuses the existing V3 Notion and V4 WeChat Reading
  commands without weakening their plans, previews, state, or transactions;
- a unified private preview index linking the latest original, Notion, and
  WeChat Reading preview results;
- explicit apply for reviewed content only;
- one status view for drafts, reviewed imports, conflicts, failures, and the
  last public writing build;
- one build command for the complete existing static site;
- per-article build isolation already provided by the V2 publisher;
- safe machine-readable workbench state and reports below ignored `build/`;
- local-only tests and fixtures that are removed before commit.

V5 does not include:

- a browser editor, public CMS, login, authentication, or remote write API;
- direct Notion API access, WeChat login, cookies, or cloud model calls;
- changes to the public visual design; that is V6;
- RSS/Atom, related articles, reading progress, or analytics;
- the deferred `跑得还快` map, city boundaries, photographs, or map services;
- fake public articles or changes to `README.md`.

## Approach choice

Three approaches were considered:

1. **Thin local orchestration over the existing adapters — selected.** It gives
   the user one workflow while preserving the independently reviewed V2, V3,
   and V4 safety boundaries.
2. **Rewrite all sources behind a new generic importer — rejected.** Notion and
   WeChat Reading have different identity, conversion, model, and recovery
   rules. A generic rewrite would erase useful constraints and duplicate proven
   transaction code.
3. **Build a local web CMS — rejected for V5.** It adds a server, CSRF and
   authentication decisions, browser file access, and a second UI before there
   is enough real content to justify that attack surface.

The workbench is therefore an application layer, not a new publishing engine.

## Workspace rules and ownership

Before implementation, `src/writings/workbench/AGENTS.md` defines the new
directory contract:

- `cli.py` owns arguments, safe terminal feedback, and exit-code mapping only;
- `models.py` owns immutable source/status/result contracts and safe errors;
- `drafts.py` owns original draft creation, validation, fingerprinting, preview,
  and guarded promotion;
- `adapters.py` owns calls into the existing Notion and WeChat Reading CLIs;
- `status.py` owns redacted aggregation of private plans/reports/state;
- `preview.py` owns only the unified private preview index;
- `build.py` owns the canonical full-site generator invocation.

The workbench may depend on public APIs from `writings`,
`writings.importers`, and `papers.site`. Existing importers and the publisher
must not depend on the workbench.

Private workbench data uses only:

```text
build/writings-workbench/
├── drafts/<slug>/
├── previews/original/<slug>/
├── preview/index.html
├── state.json
└── transactions/
```

Reports remain in their established paths:

```text
build/reports/writings-workbench.json
build/reports/notion-import.json
build/reports/weread-import.json
build/reports/writings.json
```

Only explicit apply may modify `content/writings/<slug>/`. Only the existing
publisher may modify `docs/writings/` and the public search index.

## Command contract

### `status`

```text
python -m writings.workbench status [--json]
```

The default view prints compact counts followed by actionable rows. `--json`
writes a versioned JSON document to stdout for automation. Both forms expose
only repository-relative private paths, slugs, adapter names, stable statuses,
safe issue codes, and safe remediation text.

Status aggregates:

- original drafts: `draft`, `ready`, `unchanged`, or `conflict`;
- latest Notion and WeChat Reading candidate counts from their private reports;
- last public writing build: published, retained, skipped, removed, and issues;
- missing previews or plans as `not-started`, not as failures;
- malformed or unsafe private evidence as `attention`, without guessing.

It never reads article bodies, raw exports, prompts, model responses, Notion
IDs, WeChat book IDs, credentials, or environment values for display.

### `new`

```text
python -m writings.workbench new SLUG --title TITLE \
  [--kind learning-note|book-note] [--date YYYY-MM-DD]
```

`kind` defaults to `learning-note`; date defaults to the current local date.
The command creates exactly one private bundle at
`build/writings-workbench/drafts/<slug>/index.md` and never opens an editor.
The template uses the existing front-matter vocabulary, `source: original`, an
empty summary, an empty tag list, and a short body prompt. It is intentionally
not publishable until the user edits the required summary, tags, and body.

Slug validation is the existing lowercase ASCII kebab-case contract. Existing
drafts, links, junctions, reparse points, and paths colliding under portable
case comparison are refused. The command never overwrites a draft or public
article.

### `import`

```text
python -m writings.workbench import notion EXPORT
python -m writings.workbench import weread EXPORT
```

These are safe aliases for each adapter's inspect phase using canonical plans:

- `build/notion-import/plan.yaml`;
- `build/weread-import/plan.yaml`.

The workbench forwards the adapter's exact exit status and safe message. Plans
still default every candidate to excluded. `import` never previews, calls a
model, applies content, or builds the site.

### `preview`

```text
python -m writings.workbench preview original SLUG
python -m writings.workbench preview notion EXPORT
python -m writings.workbench preview weread EXPORT --model MODEL \
  [--base-url URL] [--timeout SECONDS] [--refresh-summary]
```

Original preview validates the private bundle through the existing strict
writing catalog and renderer, copies only referenced local assets into a
private preview, and records the exact reviewed bundle fingerprint. A broken
draft produces a safe issue and leaves the last valid preview untouched.

Notion and WeChat Reading preview dispatch to their existing canonical plans
and preview implementations. The WeChat Reading loopback-only model rules and
content-addressed cache remain unchanged.

After a successful or recoverably degraded source preview, the workbench
rebuilds `build/writings-workbench/preview/index.html`. The index contains only
source names, counts, statuses, slugs, safe messages, and relative links to
existing private preview pages. It does not copy raw content from one adapter
into another preview tree.

For paper-reading inference outside this CLI, the established local runtime
must acquire arXiv HTML first and use PDF only when HTML is unavailable. V5
does not add a second downloader or change that HTML-first contract.

### `apply`

```text
python -m writings.workbench apply original SLUG
python -m writings.workbench apply notion EXPORT
python -m writings.workbench apply weread EXPORT
```

Apply never creates a preview and never calls a model. Notion and WeChat
Reading dispatch to their existing reviewed apply phases.

Original apply requires:

- a valid current draft;
- a recorded successful preview for the same exact bundle fingerprint;
- `public: true`, `source: original`, and a slug matching the directory;
- an absent public target for first apply; or
- a public target whose current fingerprint equals the last workbench-written
  fingerprint for a later update.

Human-edited public content becomes `conflict` and is never overwritten.
Promotion uses a lock, write-ahead journal, same-volume temporary bundle,
durable rename, private state update, and rollback. If recovery cannot prove a
single authoritative copy, apply returns `recovery_required` and preserves the
evidence. Apply does not run the public site build.

### `build`

```text
python -m writings.workbench build
```

Build invokes `papers.site.generate_site` with the repository's canonical
inputs and outputs:

- `docs/togos-papers.json`;
- `data/arxiv-candidates.json`;
- `config/milestone_models.yaml`;
- `content/writings/`;
- `docs/index.html`, `docs/search-index.json`, and `docs/writings/`;
- `build/reports/writings.json`.

The V2 publisher remains responsible for article-level isolation: a new broken
article is skipped, a previously published broken article retains its last
known-good output, and later articles continue. Unsafe global output,
manifest, or search failures still stop the build. The CLI returns `3` for a
degraded but safely committed build, `2` for a global failure, and `0` for a
clean build.

## Exit codes and feedback

All workbench commands use:

- `0`: requested work completed cleanly;
- `2`: invalid arguments, unsafe/global input, or failure before a safe result;
- `3`: recoverable candidate failure, conflict, or degraded build requiring
  attention while independent work completed safely.

Messages lead with the result and end with one next action. They do not print
stack traces by default. Unexpected programmer errors are not reclassified as
user mistakes.

## Privacy and path safety

- Every mutable private path is canonical and below one approved `build/`
  namespace.
- The workbench rejects absolute output overrides, traversal, links, junctions,
  reparse points, unsafe Windows names, and portable-name collisions.
- Absolute source export paths are accepted as command inputs but never stored
  in state/reports or echoed after parsing.
- JSON output and reports contain no source text, article body, model output,
  credentials, environment values, external IDs, or absolute paths.
- The unified preview is private and must never be copied into `docs/`.
- No command performs non-loopback model traffic; only the existing WeChat
  preview may perform loopback traffic.

## Failure isolation

Failures are isolated by source and item:

- one invalid original draft does not alter its prior preview or public bundle;
- one Notion page or WeChat book follows its adapter's existing per-candidate
  continuation rules;
- one public writing build failure follows the V2 skip/retain rules;
- a failed adapter dispatch does not mutate another adapter's plan, preview,
  state, or report;
- a failed unified-preview rebuild leaves the prior unified preview intact.

Unsafe state, journal, lock, containment, manifest, or rollback evidence is a
global failure. The workbench stops rather than guessing.

## Validation

Local-only tests cover:

1. CLI parsing, canonical defaults, safe feedback, and exact exit codes for all
   six commands.
2. Original draft creation, portable slug conflicts, no-overwrite behavior,
   invalid-to-ready editing, deterministic preview, and local assets.
3. Exact-preview fingerprint enforcement, first apply, unchanged reapply,
   safe update, human-edit conflict, lock contention, rollback, and crash
   recovery.
4. Notion and WeChat Reading dispatch arguments, canonical plan paths, adapter
   exit-code preservation, and isolation between adapter namespaces.
5. Status aggregation for absent, valid, degraded, malformed, and unsafe
   private evidence; JSON privacy scanning rejects bodies and absolute paths.
6. Unified preview links only to existing private pages and atomically retains
   the last valid index on failure.
7. A full local workflow for original, Notion, and WeChat Reading fixtures,
   followed by two deterministic public builds and title-search verification.
8. One broken article does not block another article and is recorded in
   `build/reports/writings.json`.
9. Python compilation, JavaScript syntax, generated public-path privacy scan,
   `git diff --check`, and a clean staged diff.

All new tests, fixtures, previews, plans, reports, drafts, generated example
articles, caches, locks, journals, and Python caches are deleted before every
commit. `README.md` remains unchanged.

## Acceptance criteria

V5 is complete when:

- the six commands are available through `python -m writings.workbench`;
- a new original draft cannot publish until its exact content has previewed;
- Notion and WeChat Reading can be inspected, previewed, and applied through
  the unified command without behavior drift in their original CLIs;
- status gives one redacted, actionable view of all three sources and the last
  site build;
- build preserves V2 per-article continuation and produces the existing public
  pages/search contract;
- no private content or local path enters Git or `docs/`;
- no browser service or public management endpoint is introduced;
- implementation is split into independently reviewable English commits;
- all temporary tests and runtime artifacts are absent from commits;
- `README.md` is byte-for-byte unchanged.

## Rollout boundary

V5 ships a local CLI and private workflow only. It does not schedule imports or
model calls. V6 starts only after V5 is verified and pushed, and is dedicated
to the public site's typography, spacing, navigation hierarchy, and responsive
visual polish. The travel map remains deferred.
