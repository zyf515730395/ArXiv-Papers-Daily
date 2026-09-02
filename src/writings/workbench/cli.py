"""Safe command-line interface for the local knowledge workbench."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
from typing import Sequence

from writings.importers.models import PROJECT_ROOT

from .adapters import apply_adapter, inspect_adapter, preview_adapter
from .build import build_site
from .drafts import apply_original, create_draft, preview_original
from .models import WorkbenchError
from .preview import rebuild_preview_index
from .status import collect_status, render_status, serialize_status


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: invalid arguments; run --help for supported options\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="python -m writings.workbench")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="show redacted local workflow status")
    status.add_argument("--json", action="store_true", dest="json_output")

    new = commands.add_parser("new", help="create a private original draft")
    new.add_argument("slug", metavar="SLUG")
    new.add_argument("--title", required=True, metavar="TITLE")
    new.add_argument(
        "--kind", choices=("learning-note", "book-note"), default="learning-note"
    )
    new.add_argument("--date", dest="published_at", metavar="YYYY-MM-DD")

    import_command = commands.add_parser("import", help="inspect a local writing export")
    import_sources = import_command.add_subparsers(dest="source", required=True)
    for source in ("notion", "weread"):
        source_parser = import_sources.add_parser(source)
        source_parser.add_argument("export", metavar="EXPORT")

    preview = commands.add_parser("preview", help="build a private reviewed preview")
    preview_sources = preview.add_subparsers(dest="source", required=True)
    preview_original_parser = preview_sources.add_parser("original")
    preview_original_parser.add_argument("slug", metavar="SLUG")
    preview_notion = preview_sources.add_parser("notion")
    preview_notion.add_argument("export", metavar="EXPORT")
    preview_weread = preview_sources.add_parser("weread")
    preview_weread.add_argument("export", metavar="EXPORT")
    preview_weread.add_argument("--model", metavar="MODEL")
    preview_weread.add_argument("--base-url", metavar="URL")
    preview_weread.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS")
    preview_weread.add_argument("--refresh-summary", action="store_true")

    apply = commands.add_parser("apply", help="apply exact reviewed content")
    apply_sources = apply.add_subparsers(dest="source", required=True)
    apply_original_parser = apply_sources.add_parser("original")
    apply_original_parser.add_argument("slug", metavar="SLUG")
    for source in ("notion", "weread"):
        source_parser = apply_sources.add_parser(source)
        source_parser.add_argument("export", metavar="EXPORT")
    commands.add_parser("build", help="build the complete public static site")
    return parser


def _display(path: Path) -> str:
    return path.resolve().relative_to(Path(PROJECT_ROOT).resolve()).as_posix()


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code or 0)

    try:
        if args.command == "preview" and args.source == "original":
            result = preview_original(args.slug)
            rebuild_preview_index()
            print(
                f"Original preview: slug={result.slug} status={result.status}; "
                f"preview: build/writings-workbench/previews/original/{result.slug}/index.html"
            )
            return 3 if result.status in {"blocked", "conflict"} else 0
        if args.command == "apply" and args.source == "original":
            result = apply_original(args.slug)
            print(
                f"Original apply: slug={result.slug} status={result.status}; "
                "report: build/reports/writings-workbench.json"
            )
            return 3 if result.status in {"blocked", "conflict"} else 0
        if args.command == "import":
            return inspect_adapter(args.source, args.export)
        if args.command == "preview" and args.source in {"notion", "weread"}:
            code = preview_adapter(
                args.source,
                args.export,
                model=getattr(args, "model", None),
                base_url=getattr(args, "base_url", None),
                timeout=getattr(args, "timeout", 30.0),
                refresh=getattr(args, "refresh_summary", False),
            )
            if code in {0, 3}:
                rebuild_preview_index()
            return code
        if args.command == "apply" and args.source in {"notion", "weread"}:
            return apply_adapter(args.source, args.export)
        if args.command == "status":
            status = collect_status()
            print(
                serialize_status(status) if args.json_output else render_status(status),
                end="",
            )
            return 0
        if args.command == "build":
            result = build_site()
            print(
                "Public build: "
                f"status={result.status} published={result.published} "
                f"retained={result.retained} skipped={result.skipped} "
                f"removed={result.removed}; report: build/reports/writings.json"
            )
            return 3 if result.status == "degraded" else 0
        if args.command != "new":
            raise WorkbenchError(
                "not_implemented", f"{args.command} is not available in this implementation slice"
            )
        try:
            published_at = (
                date.fromisoformat(args.published_at) if args.published_at else date.today()
            )
        except ValueError as error:
            raise WorkbenchError(
                "invalid_date", "date must be a valid ISO date; use YYYY-MM-DD"
            ) from error
        bundle = create_draft(args.slug, args.title, args.kind, published_at)
        print(f"Draft created: {_display(bundle / 'index.md')}; edit it, then run preview.")
        return 0
    except WorkbenchError as error:
        print(f"Workbench {args.command} failed: {error.message}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run())
