# WeChat Reading adapter rules

## File ownership

- `models.py` owns immutable private normalization and plan contracts.
- `markdown.py` owns bounded, local Markdown normalization only.
- `planner.py` owns deterministic private inspection and strict plan I/O.
- `__main__.py` owns CLI argument parsing and safe feedback only.
- Future prompt, loopback client, cache, preview, and apply modules own their own
  boundaries; this adapter must not depend on the Notion adapter.

## Closed input vocabulary

Only these metadata aliases are recognized: `title`, `book`, `bookName`;
`author`, `authors`, `writer`; `bookId`, `book_id`; `category`; and
`lastNoteUpdate`, `updated`, `updated_at`.

Only these top-level section aliases are recognized:

- highlights: `划线`, `划线笔记`, `高亮`, `高亮划线`
- thoughts: `想法`, `我的想法`, `读书笔记`
- reviews: `书评`, `本书评论`, `点评`

Unknown metadata and sections are private and excluded rather than guessed.

## Privacy and side-effect boundary

Raw notes, source paths, book IDs, timestamps, prompts, and model responses
never leave private memory or `build/weread-import/`. The adapter accepts only
local exports and future model traffic is loopback-only. No module here may
write public content or `docs/`; only the future apply orchestrator may promote
reviewed public bundles through the shared transaction boundary.
