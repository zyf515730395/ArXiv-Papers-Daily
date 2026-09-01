# WeChat Reading Summary Import V4 Design

## Purpose

V4 turns local WeChat Reading note exports into reviewable `book-note` writing bundles. It accepts files already exported to the user's machine, asks an optional model service running through WSL to synthesize the notes, previews every candidate privately, and publishes only an explicitly applied result.

The user-facing goal is one repeatable flow:

1. export books as Markdown;
2. inspect and choose books;
3. preview locally generated summaries;
4. apply the exact reviewed bundles;
5. let the existing writings publisher build the public site.

One broken book never blocks the remaining books. No WeChat credential, raw export, prompt, model response, private identifier, absolute path, or local report may enter Git or `docs/`.

## Evidence and approach choice

Current WeChat Reading export tools commonly emit one Markdown file per book with YAML metadata and sections for highlights, thoughts, and reviews. Examples include `weread-export`, `weread-obsidian`, and `weread-import`. Their exact labels vary, but their structural contract is compatible enough for a tolerant local Markdown adapter.

Ollama exposes an OpenAI-compatible local API. A loopback-only client therefore supports Ollama and equivalent WSL services without binding this repository to one runner or adding an SDK dependency.

Three approaches were considered:

1. **Local Markdown adapter plus loopback model client — selected.** It keeps authentication outside the repository, works with several exporters, and gives the user a stable private review boundary.
2. **Direct WeChat Reading Gateway integration — rejected for V4.** It adds account secrets, external API drift, rate limits, and network failure to a static-site build tool.
3. **Cookie or browser automation — rejected.** It is brittle, difficult to test deterministically, and has a weaker privacy boundary.

References:

- <https://github.com/chanity256/weread-export>
- <https://github.com/Yant2023/weread-obsidian>
- <https://github.com/gnixner/weread-import>
- <https://docs.ollama.com/api/openai-compatibility>

## Scope

V4 includes:

- safe inspection of a directory or ZIP containing UTF-8 Markdown exports;
- tolerant normalization of common book metadata and section labels;
- an editable deterministic import plan;
- loopback-only OpenAI-compatible summary generation with a private content-addressed cache;
- per-book isolation across parsing, model, validation, preview, and apply failures;
- private preview pages and a redacted machine-readable report;
- explicit transactional apply into `content/writings/<slug>/`;
- compatibility with the existing `book-note` catalog, renderer, publisher, and search index;
- local tests, local fixtures, and visual verification that are removed before commits.

V4 does not include:

- WeChat login, cookies, Gateway calls, API keys, or browser automation;
- PDF, EPUB, DOCX, HTML, or copied rich-text parsing;
- automatic publishing, scheduled model calls, CI model calls, or browser-side inference;
- cloud model endpoints or non-loopback hosts;
- public copies of raw highlights or full model transcripts;
- fabricated book notes, example public articles, cities, photographs, or map pins;
- changes to `README.md`.

## Workspace and ownership rules

Before source directories are added, the root `AGENTS.md` gains these rules:

- `src/writings/importers/weread/` owns WeChat Markdown normalization, prompt construction, loopback transport, private cache, preview orchestration, and CLI behavior.
- Reusable transaction and path-safety contracts stay in `src/writings/importers/`; the Notion and WeChat adapters may depend on them, but neither adapter depends on the other.
- Private plans, normalized notes, prompts, responses, previews, state, locks, and reports stay below ignored `build/weread-import/` or `build/reports/`.
- Only explicit apply may modify `content/writings/<slug>/`; the existing site publisher remains the sole owner of `docs/writings/` and `docs/search-index.json`.
- Tests and fixtures stay below ignored `tests/`, are deleted after verification, and are never staged or pushed.

The new adapter directory receives its own `AGENTS.md` before implementation files. It fixes module responsibilities and prevents model transport code from reading or writing public content.

## Input contract and safety

The CLI accepts either one directory or one ZIP. It reuses the V3 archive inventory safety contract:

- regular files only; links, junctions, reparse points, special entries, duplicate portable paths, unsafe Windows names, traversal, absolute paths, and file/descendant conflicts are rejected;
- at most 10,000 members, 64 MiB per member, and 1 GiB total uncompressed bytes;
- ZIP entries are bounded-streamed into a unique private run directory and cleaned after use;
- archive/global structural failures return exit `2` before plan, cache, preview, state, public content, or report mutation;
- unreadable or unsupported individual Markdown files become blocked candidates and do not stop inspection of other trustworthy files.

One Markdown file represents one book. UTF-8 with or without BOM is accepted. A candidate is recognized when a non-empty title can be obtained from YAML front matter, an H1, or a known metadata callout. If several sources disagree, front matter wins and inspection records a warning without exposing the original path.

Supported metadata aliases are deliberately small:

| Normalized field | Accepted source keys | Rule |
|---|---|---|
| title | `title`, `book`, `bookName` | required after fallback to H1/callout |
| author | `author`, `authors`, `writer` | optional plain text |
| book ID | `bookId`, `book_id` | optional private strong identity |
| category | `category` | optional plain text, never copied directly into public tags |
| updated time | `lastNoteUpdate`, `updated`, `updated_at` | optional date/time metadata |

Known top-level section aliases normalize to `highlights`, `thoughts`, and `reviews`:

- highlights: `划线`, `划线笔记`, `高亮`, `高亮划线`;
- thoughts: `想法`, `我的想法`, `读书笔记`;
- reviews: `书评`, `本书评论`, `点评`.

Chapter headings below those sections are preserved as private structure. HTML comments carrying bookmark, review, chapter, or time identifiers are removed before prompting and never enter reports. Unsupported sections remain private and are excluded from prompts instead of guessed.

## Plan and identity

`inspect EXPORT --plan build/weread-import/plan.yaml` writes a versioned deterministic plan. Each candidate contains:

- a redacted stable `source_ref` derived from a strong book ID when available, otherwise the content fingerprint;
- detected title and author;
- `include: false` by default;
- an editable lowercase ASCII kebab-case slug;
- editable public title, ISO `published_at`, one-line summary seed, and lowercase ASCII tags;
- fixed `kind: book-note` and `source: wechat-reading`.

Plans never contain absolute paths, raw notes, WeChat URLs, book IDs, timestamps identifying a note, prompts, or model output. The export fingerprint covers files only and is stable for identical bytes. Renaming a file with the same strong book ID preserves source identity; conflicting duplicate strong identities block both candidates.

Preview and apply require the exact plan version and exact export fingerprint. A changed export must be inspected again.

## Summary generation

`preview EXPORT PLAN --model MODEL [--base-url URL]` generates or reuses private summaries before rendering preview pages. Configuration can also come from `TOGOS_WSL_LLM_MODEL` and `TOGOS_WSL_LLM_BASE_URL`; command-line values take precedence. The default base URL is `http://127.0.0.1:11434/v1`.

Transport rules:

- only `http` URLs whose host is the literal `127.0.0.1`, `localhost`, or `::1` are accepted;
- userinfo, query strings, fragments, redirects, proxies, and non-default paths outside the normalized `/v1` base are rejected;
- requests use Python's standard library, connect only during preview, set a bounded timeout, and never read credentials from the plan;
- responses must be UTF-8 JSON from `/chat/completions` and stay below 4 MiB;
- model failure, timeout, malformed JSON, invalid structured output, or copyright guard failure blocks only the current book and preview continues.

The prompt receives normalized highlights, thoughts, and reviews for one selected book. Inputs are chunked at chapter/item boundaries with a 24,000-character maximum per map call. Multiple map results are reduced through the same structured contract. The model receives `temperature: 0` and a stable prompt version; no claim of byte-identical model output is made.

The required structured response is JSON:

```json
{
  "one_sentence": "one non-empty plain-text sentence",
  "key_ideas": ["three to eight concise items"],
  "reflections": ["zero to six synthesized reflections"],
  "questions": ["zero to six useful follow-up questions"]
}
```

Every string is bounded, control characters and HTML are rejected, arrays are de-duplicated, and unknown fields fail validation. Generated text is checked against normalized highlights: any output segment of 120 or more consecutive normalized characters copied from a highlight blocks the book. The public article never includes raw highlight sections automatically.

The content-addressed cache key includes source fingerprint, selected normalized content fingerprint, prompt version, model name, and transport contract version. Cache files are checksummed, written durably below `build/weread-import/cache/`, and contain no absolute path. `--refresh-summary` bypasses a valid cached response. Apply never calls the model and accepts only the exact cached summary that preview validated.

## Private preview and public article

Preview rebuilds one private site below `build/weread-import/preview/`. Each candidate has one of `ready`, `unchanged`, `conflict`, `blocked`, or `ignored`. A candidate failure is rendered as a safe explanation with a next action; other candidates continue.

A ready article bundle has the existing strict front matter:

```yaml
---
title: <public title>
slug: <slug>
published_at: <ISO date>
kind: book-note
public: true
summary: <plan seed or one_sentence>
tags: [reading, <validated plan tags>]
source: wechat-reading
---
```

The deterministic body contains only non-empty sections in this order:

1. `## 一句话`;
2. `## 核心观点`;
3. `## 我的思考`;
4. `## 留给自己的问题`.

Author may appear as a short plain-text metadata line when provided. Book ID, source filename, external cover URL, reading progress, raw highlight, raw thought, raw review, model name, and model transcript do not enter the public bundle.

The preview invokes the existing writing bundle validator and renderer. What apply promotes is byte-for-byte the reviewed bundle fingerprint, not a regenerated model answer.

## Apply, conflict handling, and recovery

V4 extracts a source-neutral prepared-bundle promotion boundary from the V3 transaction engine while preserving all Notion behavior. The shared boundary consumes validated candidate bundles, source keys, source fingerprints, reviewed written fingerprints, dependencies, canonical paths, and a private namespace. It owns locking, WAL, state transition, backups, durable replacement, recovery, report materialization, and per-candidate continuation.

Adapter code cannot choose arbitrary public or state roots. WeChat apply uses exactly:

- public content: `content/writings/`;
- private root: `build/weread-import/`;
- state: `build/weread-import/state.json`;
- lock and WAL: `build/weread-import/transactions/`;
- report: `build/reports/weread-import.json`.

First apply requires the target slug to be absent. Reapply requires a unique source mapping and a current public bundle fingerprint equal to the last written fingerprint. A human-edited public bundle becomes `conflict` and is never overwritten. A renamed export with the same strong source identity can update the existing slug; slug reassignment requires an explicit plan change and an absent new target.

Apply holds an OS-backed exclusive lock, reconciles incomplete transactions before new work, validates the entire selected group before public mutation, and writes state/report durably. One failed book is recorded and skipped while independent books continue. A clean reapply is `unchanged`. Apply returns exit `0` when every included book is applied or unchanged, `3` when at least one included book is blocked/conflicted/failed but the run remains recoverable, and `2` for a global invalid input or unsafe recovery condition.

Apply does not regenerate the site. The existing deterministic publisher remains a separate step and already skips a broken article while recording it and continuing.

## CLI feedback and privacy

Commands are:

```text
python -m writings.importers.weread inspect EXPORT --plan build/weread-import/plan.yaml
python -m writings.importers.weread preview EXPORT build/weread-import/plan.yaml --model MODEL
python -m writings.importers.weread apply EXPORT build/weread-import/plan.yaml
```

Success output reports only counts and repository-relative private destinations. Errors state what the user can do next, such as rerunning inspect after an export changed or starting the loopback model service before preview. Output and reports redact absolute paths, book IDs, source filenames, note identifiers, raw notes, prompts, and responses.

The JSON report is versioned and contains per-candidate source reference, slug, status, stable issue code, and safe message. It never contains source text or model text.

## Failure isolation

Global failures stop before mutation when archive structure, plan schema, canonical root, lock, WAL checksum, state schema, or recovery evidence is unsafe.

Candidate failures continue to the next book when a Markdown file cannot decode, metadata is insufficient, sections are unsupported, the model is unavailable, a model response is invalid, generated text copies a long highlight, a bundle fails validation, a target conflicts, or a candidate transaction rolls back successfully.

If rollback or crash recovery cannot prove the public/private state, apply stops with `recovery_required` and preserves evidence. It never guesses which copy is authoritative.

## Validation and acceptance

V4 is accepted only when all of the following are demonstrated locally:

1. Existing Notion inspect, preview, apply, concurrency, crash-recovery, scale, privacy, and deterministic publisher behavior remains unchanged after the shared promotion extraction.
2. Directory and ZIP safety covers traversal, aliases, links/reparse points, special entries, bounded streaming, duplicate identity, UTF-8 isolation, and deterministic 10,000-member inspection.
3. Representative Markdown shapes from the three referenced exporter families normalize into the same private book contract without committing their fixtures.
4. Inspect writes a deterministic redacted plan and makes no model, public-content, or site-output call.
5. The loopback client rejects cloud/private-network hosts, redirects, proxies, oversized responses, invalid JSON, unexpected fields, copied long highlights, and timeouts.
6. Cache hits avoid a model call; cache identity changes with source, selected content, prompt version, or model; corrupted cache entries are ignored safely.
7. One failed book does not stop later books in preview or apply, and the report records every included candidate.
8. Apply promotes exactly the previewed bytes, detects human edits, is idempotent, preserves first-import and renamed-export identity, serializes concurrent processes, and recovers controlled crashes at every rename/state/report boundary.
9. A real local mock OpenAI-compatible service proves map-reduce requests and structured validation without external network access.
10. A real inspect-preview-apply-reapply-publish run produces one valid `book-note`, two deterministic site generations, a working title-search document, and no leaked raw note or private identifier.
11. CLI exit codes are exactly `0`, `2`, and `3` as specified, and messages provide a safe next action.
12. All new tests, fixtures, mock services, previews, reports, caches, generated sample articles, and Python caches are deleted before every feature commit.
13. Commits are split by independently reviewable feature, use concise English messages, pass `git diff --cached --check`, and exclude `README.md`, `tests/`, `build/`, private content, and temporary output.

## Rollout boundary

V4 ships the importer and local CLI without a scheduled workflow. The user supplies real exports and chooses the WSL model later; until then, the public site remains unchanged.

The travel map remains deferred under the V1 contract until real public city coordinates and photographs are available. Once supplied, it becomes the next independent visual phase rather than being populated with fake data.
