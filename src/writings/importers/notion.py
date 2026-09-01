"""Command-line entry point for private offline Notion import phases."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .archive import open_export
from .models import NotionImportError, canonical_import_root, private_import_path
from .planner import inspect_export, load_import_plan, redact_source_ref, write_import_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m writings.importers.notion")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="inspect a local Notion Markdown and CSV export")
    inspect.add_argument("export", metavar="EXPORT")
    inspect.add_argument("--plan", metavar="PLAN", required=True)
    return parser


def _build_root(plan: str | Path) -> Path:
    try:
        private_import_path(plan)
        return canonical_import_root()
    except NotionImportError as error:
        raise NotionImportError("invalid_plan", "import plan must be below build/notion-import") from error


def _display_plan(path: str | Path) -> str:
    target = Path(path).resolve()
    root = _build_root(target)
    try:
        return target.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return (Path("build") / "notion-import" / target.relative_to(root)).as_posix()


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command != "inspect":
            raise NotionImportError("invalid_plan", "unsupported import command")
        root = _build_root(args.plan)
        plan_path = Path(args.plan)
        previous = load_import_plan(plan_path) if plan_path.exists() else None
        with open_export(args.export, root) as inventory:
            plan = inspect_export(inventory, previous)
        write_import_plan(plan_path, plan)
    except NotionImportError as error:
        print(f"Notion inspect failed: {error.message}", file=sys.stderr)
        return 2
    except OSError:
        print("Notion inspect failed: unable to inspect local export", file=sys.stderr)
        return 2
    selected = sum(item.include for item in plan.articles)
    print(f"Discovered {len(plan.articles)} Markdown pages; selected {selected}; plan: {_display_plan(plan_path)}")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
