# Writings importer module rules

## Ownership

- `archive.py` owns offline ZIP/directory validation, extraction, and immutable source inventory only.
- `planner.py` owns import-plan validation, serialization, candidate discovery, and plan persistence only.
- `notion.py` owns argument parsing and compact user-facing summaries only; it must not contain archive or plan rules.
- `models.py` owns immutable importer contracts and safe importer errors only.
- Later modules own their named responsibilities: `notion_markdown.py` conversion, `state.py` private state, and `promoter.py` guarded bundle promotion.

## Paths and privacy

- Import plans, mappings, previews, reports, state, and extraction runs are private local artifacts under ignored `build/notion-import/` or `build/reports/` only.
- Public paths are modified only by an explicit apply phase and only below `content/writings/<slug>/`.
- Do not emit absolute paths, Notion IDs, archive bytes, article bodies, environment values, or stack traces in normal terminal output or serialized public-facing diagnostics.

## Safety and execution

- This package performs no network access.
- Archive and directory traversal must validate containment and refuse links, junctions, reparse points, traversal, and ambiguous path aliases before reading or writing content.
- Any future apply implementation must persist trusted state before finalizing content replacement, and must restore the prior content if state persistence fails.

## Local verification hygiene

- New importer tests, fixtures, plans, reports, previews, extraction directories, and caches are local-only under ignored paths and must be deleted before each commit.
- Never stage or commit local test artifacts, generated output, or `README.md` changes.
