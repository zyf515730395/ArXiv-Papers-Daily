"""Thin dispatch into the established Notion and WeChat Reading CLIs."""

from __future__ import annotations

from typing import Literal

from writings.importers import notion
from writings.importers.weread import __main__ as weread_cli

from .models import WorkbenchError


AdapterName = Literal["notion", "weread"]
_PLANS = {
    "notion": "build/notion-import/plan.yaml",
    "weread": "build/weread-import/plan.yaml",
}


def _runner(source: AdapterName):
    if source == "notion":
        return notion.run
    if source == "weread":
        return weread_cli.run
    raise WorkbenchError("invalid_source", "source must be notion or weread")


def inspect_adapter(source: AdapterName, export: str) -> int:
    return _runner(source)(["inspect", export, "--plan", _PLANS[source]])


def preview_adapter(
    source: AdapterName,
    export: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 30.0,
    refresh: bool = False,
) -> int:
    arguments = ["preview", export, _PLANS[source]]
    if source == "notion":
        if model is not None or base_url is not None or timeout != 30.0 or refresh:
            raise WorkbenchError(
                "invalid_option", "model options are available only for WeChat Reading"
            )
    else:
        if model is not None:
            arguments.extend(["--model", model])
        if base_url is not None:
            arguments.extend(["--base-url", base_url])
        arguments.extend(["--timeout", str(timeout)])
        if refresh:
            arguments.append("--refresh-summary")
    return _runner(source)(arguments)


def apply_adapter(source: AdapterName, export: str) -> int:
    return _runner(source)(["apply", export, _PLANS[source]])
