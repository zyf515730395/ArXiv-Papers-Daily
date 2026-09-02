# Local knowledge workbench rules

## Ownership

- `cli.py` owns argument parsing, safe terminal feedback, and exit-code mapping only.
- `models.py` owns immutable workbench contracts and safe errors only.
- `paths.py` owns fixed private-path validation only.
- `drafts.py` owns original draft creation, validation, fingerprinting, preview, and guarded promotion.
- `adapters.py` owns direct calls into the existing Notion and WeChat Reading CLIs.
- `status.py` owns redacted status aggregation and serialization only.
- `preview.py` owns the unified private preview index only.
- `build.py` owns the canonical full-site generator invocation only.

Existing publishers and importers must not depend on this package.

## Privacy and paths

- Mutable workbench data stays below ignored `build/writings-workbench/` or `build/reports/`.
- Only explicit apply may modify `content/writings/<slug>/`.
- Only the existing site publisher may modify `docs/writings/` and `docs/search-index.json`.
- Never serialize or print absolute paths, article bodies, raw exports, source IDs, prompts, model responses, credentials, environment values, or stack traces in normal feedback.
- The workbench starts no server and performs no network traffic. Only the existing WeChat Reading preview may use its loopback-only model client.

## Verification hygiene

- Tests, fixtures, drafts, previews, reports, state, locks, journals, and generated example articles are local-only and must be deleted before each commit.
- Never stage or commit `tests/`, `build/`, private content, or `README.md` changes.
