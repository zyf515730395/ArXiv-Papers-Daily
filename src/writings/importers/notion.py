"""Command-line entry point for private offline Notion import phases."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence

from .archive import open_export
from .models import PROJECT_ROOT, NotionImportError, canonical_import_root, private_import_path
from .planner import (
    canonical_preview_root,
    inspect_export,
    load_import_plan,
    preview_import,
    write_import_plan,
)
from .promoter import apply_import
from .state import load_import_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m writings.importers.notion")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="inspect a local Notion Markdown and CSV export")
    inspect.add_argument("export", metavar="EXPORT")
    inspect.add_argument("--plan", metavar="PLAN", required=True)
    preview = commands.add_parser("preview", help="build a private local writings preview")
    preview.add_argument("export", metavar="EXPORT")
    preview.add_argument("plan", metavar="PLAN")
    apply = commands.add_parser("apply", help="apply guarded writings bundles")
    apply.add_argument("export", metavar="EXPORT")
    apply.add_argument("plan", metavar="PLAN")
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
    command = "import"
    try:
        args = parser.parse_args(argv)
        command = args.command
        if args.command == "inspect":
            root = _build_root(args.plan)
            plan_path = Path(args.plan)
            previous = load_import_plan(plan_path) if plan_path.exists() else None
            state_path = Path(PROJECT_ROOT) / "build" / "notion-import" / "state.json"
            state = load_import_state(state_path) if os.path.lexists(state_path) else None
            with open_export(args.export, root) as inventory:
                plan = inspect_export(inventory, previous, state)
            write_import_plan(plan_path, plan)
        elif args.command == "preview":
            root = _build_root(args.plan)
            plan_path = Path(args.plan)
            plan = load_import_plan(plan_path)
            report_path = Path(PROJECT_ROOT) / "build" / "reports" / "notion-import.json"
            with open_export(args.export, root) as inventory:
                result = preview_import(
                    inventory, plan, canonical_preview_root(), report_path
                )
        elif args.command == "apply":
            root = _build_root(args.plan)
            plan_path = Path(args.plan)
            plan = load_import_plan(plan_path)
            with open_export(args.export, root) as inventory:
                result = apply_import(
                    inventory,
                    plan,
                    Path(PROJECT_ROOT) / "content" / "writings",
                    Path(PROJECT_ROOT) / "build" / "notion-import" / "state.json",
                    Path(PROJECT_ROOT) / "build" / "notion-import",
                    Path(PROJECT_ROOT) / "build" / "reports" / "notion-import.json",
                )
        else:
            raise NotionImportError("invalid_plan", "unsupported import command")
    except NotionImportError as error:
        print(f"Notion {command} failed: {error.message}", file=sys.stderr)
        return 3 if error.code in {"recovery_required", "recovery_failed"} else 2
    except OSError:
        print(f"Notion {command} failed: unable to read local import input", file=sys.stderr)
        return 2
    if args.command in {"preview", "apply"}:
        counts = result.counts()
        summary = "; ".join(
            f"{status}={count}" for status, count in counts.items()
        )
        if args.command == "preview":
            print(
                f"Preview: {summary}; preview: {_display_plan(canonical_preview_root())}; "
                "report: build/reports/notion-import.json"
            )
        else:
            print(f"Apply: {summary}; report: build/reports/notion-import.json")
        return 0
    selected = sum(item.include for item in plan.articles)
    print(f"Discovered {len(plan.articles)} Markdown pages; selected {selected}; plan: {_display_plan(plan_path)}")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
