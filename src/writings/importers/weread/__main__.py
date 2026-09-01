"""Command-line entry point for private WeChat Reading inspection."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence

from ..archive import open_export
from ..models import PROJECT_ROOT, WEREAD_NAMESPACE, WeReadImportError, canonical_private_root, private_import_path
from .planner import inspect_export, write_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m writings.importers.weread")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="inspect a local WeChat Reading Markdown export")
    inspect.add_argument("export", metavar="EXPORT")
    inspect.add_argument("--plan", metavar="PLAN", required=True)
    for name in ("preview", "apply"):
        reserved = commands.add_parser(name, help="not implemented yet")
        reserved.add_argument("export", metavar="EXPORT")
        reserved.add_argument("plan", metavar="PLAN")
    return parser


def _display_plan(path: str | Path) -> str:
    target = private_import_path(path, WEREAD_NAMESPACE)
    root = canonical_private_root(WEREAD_NAMESPACE)
    return (Path("build") / "weread-import" / target.relative_to(root)).as_posix()


def _preflight_plan(path: str | Path) -> Path:
    """Reject lexical plan escapes before archive validation can create private output."""
    root = Path(PROJECT_ROOT).resolve() / "build" / "weread-import"
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise WeReadImportError("invalid_plan", "plan must be below build/weread-import") from error
    if not relative.parts:
        raise WeReadImportError("invalid_plan", "plan must name a file below build/weread-import")
    return candidate


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command != "inspect":
            parser.error(f"{args.command} is not implemented yet; use inspect to create a private plan")
        requested_plan = _preflight_plan(args.plan)
        private_root = Path(PROJECT_ROOT).resolve() / "build" / "weread-import"
        with open_export(args.export, private_root, namespace=WEREAD_NAMESPACE) as inventory:
            plan = inspect_export(inventory)
            blocked = len(inventory.markdown_paths) - len(plan.books)
        plan_path = private_import_path(requested_plan, WEREAD_NAMESPACE)
        write_plan(plan_path, plan)
    except WeReadImportError as error:
        print(f"WeChat Reading inspect failed: {error.message}. Export a safe local directory or ZIP and rerun inspect.", file=sys.stderr)
        return 2
    except OSError:
        print("WeChat Reading inspect failed: unable to read local input. Export a safe local directory or ZIP and rerun inspect.", file=sys.stderr)
        return 2
    print(f"Discovered {len(plan.books)} books; blocked {blocked}; plan: {_display_plan(plan_path)}")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
