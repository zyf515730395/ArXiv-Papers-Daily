"""Fixed private-path boundaries for the local knowledge workbench."""

from __future__ import annotations

import os
from pathlib import Path

from writings.importers.models import PROJECT_ROOT

from .models import WorkbenchError


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction and is_junction():
            return True
        stat = path.lstat()
        return bool(getattr(stat, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _checked(path: Path) -> Path:
    project = Path(PROJECT_ROOT).resolve()
    target = path.resolve(strict=False)
    if not target.is_relative_to(project / "build"):
        raise WorkbenchError("unsafe_path", "private workbench path escaped build")
    current = project
    for component in target.relative_to(project).parts:
        current = current / component
        if os.path.lexists(current) and _is_link(current):
            raise WorkbenchError("unsafe_path", "private workbench path contains a link")
    return target


def workbench_root() -> Path:
    return _checked(Path(PROJECT_ROOT) / "build" / "writings-workbench")


def draft_root() -> Path:
    return _checked(workbench_root() / "drafts")


def preview_root() -> Path:
    return _checked(workbench_root() / "preview")


def original_previews_root() -> Path:
    return _checked(workbench_root() / "previews" / "original")


def state_path() -> Path:
    return _checked(workbench_root() / "state.json")


def review_path() -> Path:
    return _checked(workbench_root() / "review.json")


def report_path() -> Path:
    return _checked(Path(PROJECT_ROOT) / "build" / "reports" / "writings-workbench.json")
