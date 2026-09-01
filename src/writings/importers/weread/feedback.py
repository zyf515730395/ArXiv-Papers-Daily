"""Stable next-step guidance for private WeRead import failures."""

from __future__ import annotations


def remediation_for(code: str) -> str:
    """Return one phase-specific, privacy-safe next action."""
    if code == "invalid_model":
        return "Use a trimmed model name of at most 200 characters, then rerun preview."
    if code in {"invalid_summary", "copyright_guard"}:
        return "Adjust the local model or selected notes, then rerun preview."
    if code.startswith("model_") or code in {
        "invalid_model_url",
        "invalid_model_request",
    }:
        return "Start or configure the local WSL model service, then rerun preview."
    if code in {"cache_read_failed", "cache_write_failed", "invalid_cache_key"}:
        return "Check private summary cache permissions and free space, then rerun preview."
    if code in {"bundle_write_failed", "bundle_verify_failed"}:
        return "Check private bundle storage permissions and free space, then rerun preview."
    if code == "preview_render_failed":
        return "Check local site templates and renderer, then rerun preview."
    if code == "preview_assets_unavailable":
        return "Restore the local site assets, then rerun preview."
    if code in {
        "preview_write_failed",
        "preview_stage_failed",
        "preview_swap_failed",
        "review_state_write_failed",
        "report_write_failed",
    }:
        return "Check private preview storage permissions and free space, then rerun preview."
    if code == "local_io_failed":
        return "Check local filesystem permissions and free space, then retry."
    if code in {"cache_changed", "review_changed", "missing_preview"}:
        return "Rerun preview to rebuild the reviewed private artifacts, then try again."
    if code == "invalid_plan":
        return "Rerun inspect, review the private plan, then try again."
    if code == "bundle_invalid":
        return "Fix the private plan or model output, then rerun preview."
    if code in {
        "unreadable_source",
        "invalid_utf8",
        "invalid_markdown",
        "invalid_metadata",
        "missing_metadata",
        "source_item_too_large",
        "summary_source_empty",
    }:
        return "Fix the local export, then rerun inspect."
    return "Check the safe local export and private plan, then retry."


__all__ = ["remediation_for"]
