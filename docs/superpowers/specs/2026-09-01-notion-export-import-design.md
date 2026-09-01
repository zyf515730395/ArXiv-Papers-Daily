# Notion Export Import V3 Design

## Summary

V3 adds an offline, preview-first Notion ingestion adapter for `谈笑风生`. It accepts an official Notion `Markdown & CSV` ZIP archive or an already extracted directory, discovers Markdown pages and their local files, converts explicitly selected pages into the repository's existing `content/writings/<slug>/` contract, and protects both private Notion identity and human edits.

The importer never participates in public site generation. It writes previews and private synchronization state only below ignored `build/`, and it promotes a bundle into `content/writings/` only after the candidate passes the same strict article contract used by the V2 publisher. The public repository remains the sole source of truth after import.

## Goals

- Reuse existing Notion notes without adding Notion credentials or network access to the repository or CI.
- Accept a Notion `Markdown & CSV` ZIP archive or extracted export directory.
- Discover candidate pages without assuming undocumented Notion filename conventions.
- Provide a small editable import plan that separates private source identity from public article metadata.
- Generate local previews before any repository article is created or updated.
- Convert selected Markdown, local images, and resolvable internal page links into valid writing bundles.
- Keep article slugs stable across repeated exports and title changes.
- Make repeated imports idempotent and refuse to overwrite human-edited repository content.
- Isolate failures per page: valid candidates continue while invalid candidates are skipped and reported.
- Keep ZIP extraction, paths, reports, state, and promotion safe on Windows and Linux.

## Non-goals

- No Notion API, OAuth, integration token, browser automation, or background synchronization.
- No automatic export download, scheduled import, or CI import job.
- No AI-generated summary, tags, title, or rewritten prose.
- No WeChat Reading parser or WSL inference service.
- No import of databases as public tables, CSV files as articles, PDFs, videos, audio, arbitrary attachments, or remote images.
- No silent publication of every page in an export.
- No automatic commit or push of imported writing.
- No private Notion page ID, workspace name, local absolute path, export timestamp, or sync fingerprint in public front matter or generated site output.
- No fake example article and no README change.

## Why export-first

Notion's official export produces Markdown for non-database pages, CSV for full-page databases, and a ZIP containing pages and included subpages. Uploaded files can be included. Notion also documents that callout blocks may be emitted as HTML because Markdown has no equivalent representation.

An export-first adapter therefore provides the narrowest reliable boundary: the user controls when data leaves Notion, the importer receives ordinary local files, and public builds stay deterministic and credential-free. A future API adapter can produce the same private import plan and reuse conversion, validation, state, and promotion without changing the publisher.

## User workflow

The importer exposes one module entry point with three explicit phases:

```text
python -m writings.importers.notion inspect <export.zip-or-directory> --plan <plan.yaml>
python -m writings.importers.notion preview <export.zip-or-directory> <plan.yaml>
python -m writings.importers.notion apply <export.zip-or-directory> <plan.yaml>
```

### 1. Inspect

`inspect` safely reads the export, discovers Markdown pages, and writes an ignored plan to `build/notion-import/plan.yaml`. It does not modify `content/` or `docs/`.

Each candidate begins with `include: false`. The importer fills only facts it can derive safely: a relative source reference, the page title, and a suggested slug. The user selects pages and supplies or reviews public metadata:

```yaml
version: 1
source: notion-export
export_fingerprint: sha256:...
articles:
  - source_ref: Notes/My Article.md
    include: true
    slug: my-article
    title: My Article
    published_at: 2026-09-01
    kind: learning-note
    summary: A concise public summary.
    tags: [diffusion, notes]
```

The plan never stores an absolute input path. Its relative `source_ref` may contain a Notion-generated page identifier because the plan itself is ignored private state; terminal summaries and JSON reports redact recognized identifiers, and no identifier enters a public bundle.

`inspect` is deterministic for the same extracted content and existing state. Re-running it updates discovered candidates while preserving reviewed metadata for a matched source.

### 2. Preview

`preview` verifies that the supplied export inventory matches the plan's `export_fingerprint`, converts every selected candidate into an ignored staging area under `build/notion-import/preview/`, validates the resulting bundle, and renders an article preview with the existing writings renderer. It does not modify the repository article source.

The command prints a compact summary and writes `build/reports/notion-import.json`. Each article is classified as:

- `ready`: conversion and strict bundle validation passed.
- `unchanged`: the export matches the last applied bundle.
- `conflict`: the target bundle changed after the last import or the slug is occupied without trusted state.
- `blocked`: metadata, content, asset, link, or format validation failed.
- `ignored`: `include` is false.

### 3. Apply

`apply` verifies the supplied export fingerprint and rebuilds candidates from the export plus plan rather than trusting stale preview files. It promotes only `ready` candidates into `content/writings/<slug>/` with per-article atomic replacement. It records the applied source identity, slug, source fingerprint, and exact written bundle fingerprint in ignored state.

An invalid candidate never blocks another valid candidate. A global archive, plan, state, or filesystem safety error stops before any article promotion. The command never runs the public site generator, commits, or pushes; the normal V2 build remains the independent publication gate.

## Architecture and directory rules

V3 introduces a dedicated adapter package:

```text
src/writings/importers/
├── AGENTS.md
├── __init__.py
├── models.py
├── archive.py
├── notion_markdown.py
├── planner.py
├── state.py
├── promoter.py
└── notion.py
```

Before source files are added, project rules are extended to define these boundaries and `src/writings/importers/AGENTS.md` is committed:

- `archive.py` owns safe ZIP/directory access and source inventory only.
- `notion_markdown.py` owns Notion-specific Markdown, image, and link conversion only.
- `planner.py` owns public metadata plans and candidate status construction.
- `state.py` owns ignored private identity and fingerprints.
- `promoter.py` owns guarded, atomic bundle promotion.
- `notion.py` owns argument parsing and user-facing summaries; it contains no conversion rules.
- Importers may call stable public validation/rendering helpers from `writings`, but the V2 publisher never imports adapter code.
- No adapter writes directly to `docs/`.

The V2 catalog exposes the smallest reusable validation surface needed to validate one prepared bundle. It does not relax the canonical source-root rule used by publication. Preview rendering uses the existing article renderer against a validated in-memory article and staged assets; it does not pretend the preview directory is a public writings source root.

## Export input and extraction safety

Supported inputs are one `.zip` file or one readable directory. ZIP handling must reject:

- absolute, drive-qualified, UNC, device, empty, duplicate, or traversal member paths;
- backslash aliases and normalized path collisions;
- symlink, junction, reparse-point, or special-file entries;
- nested ZIP auto-expansion;
- encrypted entries;
- excessive member count, single-file size, or total expanded size;
- case-folded destination collisions that would be ambiguous on Windows.

The first implementation uses explicit conservative limits in one configuration object, with actionable errors. ZIP contents are extracted only below a newly created ignored run directory. Directory inputs are inventoried without following links or junctions. Temporary extraction is removed on success and failure after its resolved path is verified below `build/notion-import/`.

CSV files are inventoried for diagnostics and future database metadata mapping but are not converted into articles in V3. Markdown candidates are not inferred from sitemap HTML.

## Candidate identity and private state

Public article identity remains the repository slug. Import identity is private and stored in `build/notion-import/state.json`:

```json
{
  "version": 1,
  "sources": {
    "<private-source-key>": {
      "slug": "my-article",
      "source_fingerprint": "sha256:...",
      "written_fingerprint": "sha256:..."
    }
  }
}
```

The importer may recognize a 32-hex Notion page identifier in an exported filename as a strong private source key, but correctness cannot depend on this undocumented convention. Without such an identifier, the normalized relative source path is the initial private key. If a later export cannot be matched unambiguously, it creates an unmatched candidate and never guesses an existing slug.

The state file contains no article body and is never staged or committed. State writes are atomic. Malformed, unsupported, duplicated, or conflicting state is a global fatal error; the importer does not reconstruct trust from public files.

## Metadata and slug behavior

All selected candidates must satisfy the V2 front matter contract before preview can be `ready`:

- `title`: required public title; initially suggested from a single leading page H1 or sanitized filename. The private plan also retains the detected source title used only for conversion.
- `slug`: lowercase ASCII kebab-case; suggested from the title plus a short deterministic suffix only when needed for uniqueness.
- `published_at`: explicitly reviewed ISO date; never guessed from ZIP timestamps or filesystem modification time.
- `kind`: explicitly `learning-note` or `book-note`.
- `summary`: explicitly reviewed one-line plain text; never synthesized from private prose.
- `tags`: explicitly reviewed non-empty normalized list.
- `public`: always literal `true` in a promoted bundle.
- `source`: always `notion`.

Once state maps a source to a slug, `inspect` reuses that slug even if the Notion title or export path changes. Changing a mapped slug requires an explicit plan migration and is blocked when the old bundle exists; V3 does not perform public URL migrations.

## Markdown conversion

Conversion is deterministic and intentionally narrow:

1. Decode UTF-8 with an optional BOM; invalid text blocks that candidate.
2. Normalize line endings to LF.
3. Detect one leading H1 as the exported page title and remove it only when it matches the plan's detected source title after Unicode normalization. A deliberately edited public title does not affect this removal. Any other parsed body H1 remains a validation error.
4. Preserve supported Markdown structures handled by the V2 renderer.
5. Convert recognized Notion callout `<aside>` blocks to blockquotes while preserving their text and safe inline Markdown. Unrecognized author HTML remains subject to the existing renderer sanitizer and produces a preview warning when content may be lost.
6. Rewrite local image references to `assets/<safe-name>` and copy their bytes into the staged bundle.
7. Rewrite links to another selected and mapped Notion page as `<target-slug>.html` with any safe fragment preserved.
8. Block unresolved local page links, local non-image attachments, remote images, and links escaping the export root. Ordinary safe web links remain unchanged.
9. Emit front matter in the existing canonical field order followed by the converted body.

Code fences and inline code are protected before link/image/HTML conversion so example syntax is never rewritten. URL decoding happens once, and containment is checked after normalization and path resolution.

## Asset handling

Only the V2-supported image types are imported: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, and `.avif`. Every source must be a regular non-link file inside the export inventory.

Destination names use a sanitized basename. Collisions receive a deterministic short content hash before the extension. Identical bytes referenced more than once in one article share one copied asset. Different articles own separate asset directories even when source bytes match.

The importer does not transcode, optimize, fetch, or inspect EXIF metadata. The existing publisher remains responsible for its normal public asset validation.

## Human-edit protection and idempotency

For a new slug, promotion requires that `content/writings/<slug>/` not exist. For an existing state-mapped slug:

1. Compute the current bundle fingerprint from normalized relative paths and file bytes.
2. Compare it with the state's `written_fingerprint`.
3. If they differ, return `conflict` and leave the bundle untouched.
4. If they match and the new candidate fingerprint is identical, return `unchanged`.
5. If they match and the candidate differs, atomically replace the bundle and update state.

There is no `--force` in V3. Resolving a conflict means intentionally reconciling the Notion source and repository bundle, choosing a new slug, or updating the private state through a future explicit adoption workflow. This makes silent loss of hand edits impossible.

Per-article promotion stages a complete sibling directory, verifies both source and destination remain inside canonical project roots, swaps through a backup, updates state atomically, and rolls back the bundle if state persistence fails. A crash may leave only safely named ignored staging/backup directories, which the next run detects and reports rather than deleting blindly.

## Diagnostics and privacy

`build/reports/notion-import.json` contains only:

- plan-relative source references with recognized Notion IDs redacted;
- public slug and status;
- stable issue codes and safe messages;
- counts for ready, unchanged, conflict, blocked, applied, and ignored candidates.

Reports, plans, previews, extraction directories, and state stay below ignored `build/`. Terminal output never includes article bodies, absolute paths, page IDs, archive member bytes, environment values, or stack traces by default.

Per-candidate issue examples include `missing_metadata`, `unsafe_source_path`, `unsupported_attachment`, `missing_image`, `unresolved_page_link`, `body_h1`, `bundle_conflict`, and `invalid_notion_html`. Global errors include `unsafe_archive`, `invalid_plan`, `invalid_state`, `ambiguous_identity`, and `promotion_failed`.

## Failure semantics

- Inspect failures are global because no trustworthy plan can be produced.
- Preview conversion and validation failures are per candidate; other selected candidates continue.
- Apply rebuilds all candidates and skips any candidate that is not currently `ready`.
- A promotion failure rolls back that candidate and continues only when rollback is proven complete.
- An unsafe root, invalid private state, ambiguous recovery residue, or failed rollback aborts the entire apply operation.
- Public `docs/writings/`, its manifest, and the global search index are never touched by the importer.

## Testing strategy

All new tests, fixtures, export archives, plans, state files, reports, previews, and snapshots remain local-only and are deleted before each commit.

The local matrix covers:

- ZIP traversal, absolute/UNC/drive paths, duplicate normalized paths, case collisions, links, special files, encryption, and size/count limits.
- Directory inputs containing junctions, symlinks, unreadable files, and nested paths.
- Flat and nested Notion Markdown exports, optional filename IDs, UTF-8 BOM, Unicode titles, and CSV inventory.
- Deterministic inspect plans and preservation of reviewed metadata.
- Leading-title H1 removal, remaining H1 rejection, code protection, callout conversion, and sanitizer warnings.
- Local image rewriting, missing/escaping images, duplicate bytes, filename collisions, and unsupported attachments.
- Selected-page link rewriting, anchors, cycles, unselected targets, ambiguous paths, and external links.
- Strict front matter output and validation through the public writings validation API.
- First apply, unchanged reapply, changed export update, missing state, occupied slug, human-edited bundle conflict, and title change with stable slug.
- Atomic promotion, state-write rollback, crash residue detection, per-page isolation, and safe reports.
- CLI exit codes and compact summaries for inspect, preview, and apply.
- End-to-end import followed by the existing site generator, proving one imported article produces the expected page, assets, manifest record, and title-search record.

Browser verification uses a local-only imported fixture and checks the article at 390, 768, and 1440 pixels in light and dark modes. The fixture and generated output are removed before commit.

## Implementation slices

1. Add importer directory rules, immutable plan/state/status models, safe archive inventory, and deterministic inspect output.
2. Add Markdown/callout/image/internal-link conversion plus reusable strict bundle validation and preview rendering.
3. Add private state, human-edit detection, atomic per-article apply, CLI summaries, reports, and end-to-end site verification.

Commits remain split by these independent behaviors. Before each commit, local tests run, all test/import/report/build artifacts are removed, staged files are inspected, `git diff --cached --check` passes, and README remains unchanged.

## Acceptance criteria

V3 is complete when:

- An official Markdown & CSV ZIP and an extracted directory produce the same deterministic candidate plan.
- Nothing is selected or published implicitly.
- A selected page with reviewed metadata previews as a valid `source: notion` article bundle.
- Local images and selected internal page links are rewritten without escaping the export or article roots.
- Re-importing unchanged content is a no-op.
- Re-importing changed content updates only a state-trusted, unedited bundle.
- Human edits, missing state, occupied slugs, unsafe archives, unresolved links, and malformed candidates never overwrite repository content.
- One candidate failure does not stop another valid candidate from applying.
- No Notion ID, credential, absolute path, private state, plan, report, preview, fake article, test artifact, or README change is committed.
- The existing V2 publisher remains independent and publishes an imported bundle without adapter-specific behavior.

## Official format reference

- Notion Help, Export your content: <https://www.notion.com/help/export-your-content>
- Notion Help, Back up your data: <https://www.notion.com/help/back-up-your-data>
