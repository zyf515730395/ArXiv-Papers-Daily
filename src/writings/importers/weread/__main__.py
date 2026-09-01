"""Command-line entry point for private WeChat Reading imports."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence

from ..archive import open_export
from ..models import (
    CANDIDATE_STATUSES,
    PROJECT_ROOT,
    WEREAD_NAMESPACE,
    WeReadImportError,
    canonical_private_root,
    private_import_path,
)
from .models import SummaryConfig
from .planner import inspect_export, load_plan, write_plan
from .workflow import apply_import, preview_import


_REPORT_DISPLAY = "build/reports/weread-import.json"


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: invalid arguments; run --help for supported options\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="python -m writings.importers.weread")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", help="inspect a local WeChat Reading Markdown export"
    )
    inspect.add_argument("export", metavar="EXPORT")
    inspect.add_argument("--plan", metavar="PLAN", required=True)

    preview = commands.add_parser(
        "preview", help="build a private preview with a loopback model"
    )
    preview.add_argument("export", metavar="EXPORT")
    preview.add_argument("plan", metavar="PLAN")
    preview.add_argument("--model", metavar="MODEL")
    preview.add_argument("--base-url", metavar="URL")
    preview.add_argument("--timeout", metavar="SECONDS", type=float, default=30.0)
    preview.add_argument("--refresh-summary", action="store_true")

    apply = commands.add_parser(
        "apply", help="apply the exact reviewed private preview"
    )
    apply.add_argument("export", metavar="EXPORT")
    apply.add_argument("plan", metavar="PLAN")
    return parser


def _display_plan(path: str | Path) -> str:
    target = private_import_path(path, WEREAD_NAMESPACE)
    root = canonical_private_root(WEREAD_NAMESPACE)
    return (Path("build") / "weread-import" / target.relative_to(root)).as_posix()


def _preflight_plan(path: str | Path) -> Path:
    """Reject lexical plan escapes before archive validation can create output."""
    root = Path(PROJECT_ROOT).resolve() / "build" / "weread-import"
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise WeReadImportError(
            "invalid_plan", "plan must be below build/weread-import"
        ) from error
    if not relative.parts:
        raise WeReadImportError(
            "invalid_plan", "plan must name a file below build/weread-import"
        )
    return candidate


def _counts(result) -> str:
    counts = result.counts()
    return " ".join(f"{status}={counts[status]}" for status in CANDIDATE_STATUSES)


def _candidate_exit(result) -> int:
    return (
        3
        if any(candidate.status in {"conflict", "blocked"} for candidate in result.candidates)
        else 0
    )


def _failure(command: str, error: WeReadImportError) -> int:
    if error.code.startswith("model_") or error.code in {
        "invalid_model",
        "invalid_model_url",
        "invalid_model_request",
        "invalid_summary",
        "copyright_guard",
    }:
        action = "Start or configure the local WSL model service, then rerun preview."
    elif error.code in {
        "invalid_plan",
        "review_changed",
        "missing_preview",
        "cache_changed",
        "cache_read_failed",
        "invalid_cache_key",
    }:
        action = "Rerun inspect or preview as requested, then try again."
    elif error.code in {
        "preview_assets_unavailable",
        "preview_stage_failed",
        "preview_render_failed",
        "preview_write_failed",
        "preview_swap_failed",
        "review_state_write_failed",
        "report_write_failed",
        "bundle_write_failed",
        "bundle_verify_failed",
        "cache_write_failed",
        "local_io_failed",
    }:
        action = "Check private build storage and local site files, then retry."
    elif error.code == "bundle_invalid":
        action = "Fix the private plan or model output, then rerun preview."
    else:
        action = "Check the safe local export and private plan, then retry."
    print(f"WeChat Reading {command} failed: {error.message}. {action}", file=sys.stderr)
    return 2


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    try:
        requested_plan = _preflight_plan(args.plan)
        private_root = Path(PROJECT_ROOT).resolve() / "build" / "weread-import"
        if command == "inspect":
            with open_export(
                args.export, private_root, namespace=WEREAD_NAMESPACE
            ) as inventory:
                plan = inspect_export(inventory)
                blocked = len(inventory.markdown_paths) - len(plan.books)
            plan_path = private_import_path(requested_plan, WEREAD_NAMESPACE)
            write_plan(plan_path, plan)
            print(
                f"Discovered {len(plan.books)} books; blocked {blocked}; plan: {_display_plan(plan_path)}"
            )
            return 0

        plan = load_plan(requested_plan)
        if command == "preview":
            model = args.model or os.environ.get("TOGOS_WSL_LLM_MODEL")
            if not model:
                print(
                    "WeChat Reading preview failed: missing --model or TOGOS_WSL_LLM_MODEL. "
                    "Start or configure the local WSL model service, then rerun preview.",
                    file=sys.stderr,
                )
                return 2
            base_url = (
                args.base_url
                or os.environ.get("TOGOS_WSL_LLM_BASE_URL")
                or "http://127.0.0.1:11434/v1"
            )
            try:
                config = SummaryConfig(model, base_url, args.timeout)
            except ValueError:
                raise WeReadImportError(
                    "invalid_model", "local model settings are invalid"
                ) from None
            with open_export(
                args.export, private_root, namespace=WEREAD_NAMESPACE
            ) as inventory:
                result = preview_import(
                    inventory, plan, config, refresh=args.refresh_summary
                )
            print(f"{_counts(result)}; report: {_REPORT_DISPLAY}")
            return _candidate_exit(result)

        with open_export(
            args.export, private_root, namespace=WEREAD_NAMESPACE
        ) as inventory:
            result = apply_import(inventory, plan)
        print(f"{_counts(result)}; report: {_REPORT_DISPLAY}")
        return _candidate_exit(result)
    except WeReadImportError as error:
        return _failure(command, error)
    except OSError:
        return _failure(
            command,
            WeReadImportError("local_io_failed", "local private I/O failed safely"),
        )


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
