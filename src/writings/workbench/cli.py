"""Safe command-line interface for the local knowledge workbench."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
from typing import Sequence

from writings.importers.models import PROJECT_ROOT

from .drafts import create_draft
from .models import WorkbenchError


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: invalid arguments; run --help for supported options\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="python -m writings.workbench")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show redacted local workflow status")

    new = commands.add_parser("new", help="create a private original draft")
    new.add_argument("slug", metavar="SLUG")
    new.add_argument("--title", required=True, metavar="TITLE")
    new.add_argument(
        "--kind", choices=("learning-note", "book-note"), default="learning-note"
    )
    new.add_argument("--date", dest="published_at", metavar="YYYY-MM-DD")

    for command, help_text in (
        ("import", "inspect a local writing export"),
        ("preview", "build a private reviewed preview"),
        ("apply", "apply exact reviewed content"),
    ):
        child = commands.add_parser(command, help=help_text)
        child.add_argument("arguments", nargs="*")
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
