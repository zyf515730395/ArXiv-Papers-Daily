"""Deterministic Notion Markdown conversion into private writing bundles."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
from typing import Mapping
from urllib.parse import unquote, urlsplit
import unicodedata

import yaml

from writings.catalog import SUPPORTED_ASSET_EXTENSIONS

from .models import (
    ConvertedBundle,
    ExportFile,
    ExportInventory,
    ImportArticlePlan,
    ImportIssue,
    NotionImportError,
    portable_collision_key,
    private_import_path,
    validate_portable_relative_path,
)


_LEADING_H1 = re.compile(
    r"\A[ \t]*#[ \t]+(.+?)[ \t]*(?:#+[ \t]*)?(?:\n|\Z)"
)
_MARKDOWN_REFERENCE = re.compile(r"(!?)\[([^\]\n]*)\]\(([^)\n]+)\)")
_INLINE_CODE = re.compile(r"(?<!`)(`+)([^\n]*?)(?<!`)\1(?!`)")
_ASIDE = re.compile(r"<aside(?:[ \t][^>]*)?>(.*?)</aside[ \t]*>", re.IGNORECASE | re.DOTALL)
_ASIDE_TAG = re.compile(r"</?aside\b", re.IGNORECASE)
_AUTHOR_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_INVALID_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SAFE_FRAGMENT = re.compile(r"^[^\s\\#\x00-\x1f\x7f]*$")
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*(?:\n|\Z)")
_SAFE_INLINE_TAGS = {"strong", "em", "code", "a", "br"}
_REPARSE_POINT = 0x0400


def _fail(code: str, message: str) -> NotionImportError:
    return NotionImportError(code, message)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(junction and junction()) or bool(
            getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
        )
    except OSError:
        return True


def _token(prefix: str, index: int, text: str) -> str:
    value = f"\ue000NOTION{prefix}{index}\ue001"
    while value in text:
        index += 1
        value = f"\ue000NOTION{prefix}{index}\ue001"
    return value


def _protect_code(text: str) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN.match(lines[index])
        if opening is None:
            output.append(lines[index])
            index += 1
            continue
        marker = opening.group(1)
        end_pattern = re.compile(
            rf"^[ \t]{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*(?:\n|\Z)"
        )
        end = index + 1
        while end < len(lines) and end_pattern.match(lines[end]) is None:
            end += 1
        if end < len(lines):
            end += 1
        block = "".join(lines[index:end])
        token = _token("FENCE", len(protected), text)
        protected[token] = block
        output.append(token)
        index = end
    fenced = "".join(output)

    def protect_inline(match: re.Match[str]) -> str:
        token = _token("INLINE", len(protected), fenced)
        protected[token] = match.group(0)
        return token

    return _INLINE_CODE.sub(protect_inline, fenced), protected


def _restore_code(text: str, protected: Mapping[str, str]) -> str:
    for token, value in protected.items():
        text = text.replace(token, value)
    return text


class _SafeAsideParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.safe = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _SAFE_INLINE_TAGS:
            self.safe = False
        elif tag == "a" and any(name not in {"href", "title"} for name, _ in attrs):
            self.safe = False
        elif tag != "a" and attrs:
            self.safe = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _SAFE_INLINE_TAGS:
            self.safe = False

    def handle_comment(self, data: str) -> None:
        self.safe = False

    def handle_decl(self, decl: str) -> None:
        self.safe = False

    def handle_pi(self, data: str) -> None:
        self.safe = False


def _convert_asides(text: str) -> str:
    def convert(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        parser = _SafeAsideParser()
        try:
            parser.feed(inner)
            parser.close()
        except (ValueError, AssertionError) as error:
            raise _fail("invalid_notion_html", "Notion callout HTML is malformed") from error
        if not parser.safe or _ASIDE_TAG.search(inner):
            raise _fail("invalid_notion_html", "Notion callout contains unsafe nested HTML")
        return "\n".join(">" if not line else f"> {line}" for line in inner.split("\n"))

    converted = _ASIDE.sub(convert, text)
    if _ASIDE_TAG.search(converted):
        raise _fail("invalid_notion_html", "Notion callout HTML is malformed")
    return converted


def _normalized_title(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().rstrip("#").strip()


def _remove_matching_h1(text: str, detected_title: str) -> str:
    match = _LEADING_H1.match(text)
    if match is not None and _normalized_title(match.group(1)) == _normalized_title(
        detected_title
    ):
        return text[match.end() :]
    return text


def _decode_component(value: str) -> str:
    if _INVALID_PERCENT.search(value):
        raise _fail("unsafe_source_path", "local reference contains invalid URL encoding")
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError as error:
        raise _fail("unsafe_source_path", "local reference is not valid UTF-8") from error


def _resolve_reference(source_ref: str, raw_path: str) -> str:
    decoded = _decode_component(raw_path)
    windows = PureWindowsPath(decoded)
    if (
        not decoded
        or "\\" in decoded
        or decoded.startswith("/")
        or windows.is_absolute()
        or windows.drive
    ):
        raise _fail("unsafe_source_path", "local reference path is unsafe")
    parts = list(PurePosixPath(source_ref).parent.parts)
    for part in PurePosixPath(decoded).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise _fail("unsafe_source_path", "local reference escapes the export root")
            parts.pop()
            continue
        parts.append(part)
    try:
        return validate_portable_relative_path(PurePosixPath(*parts).as_posix()).as_posix()
    except ValueError as error:
        raise _fail("unsafe_source_path", "local reference path is unsafe") from error


def _inventory_name(inventory: ExportInventory, reference: str) -> str | None:
    if reference in inventory.files:
        return reference
    key = portable_collision_key(reference)
    matches = [name for name in inventory.files if portable_collision_key(name) == key]
    if len(matches) > 1:
        raise _fail("ambiguous_source_path", "local reference is ambiguous")
    return matches[0] if matches else None


def _safe_fragment(fragment: str) -> str:
    if _INVALID_PERCENT.search(fragment) or not _SAFE_FRAGMENT.fullmatch(fragment):
        raise _fail("unsafe_source_path", "local reference fragment is unsafe")
    return fragment


def _read_inventory_file(record: ExportFile, inventory: ExportInventory) -> bytes:
    source = record.source_path
    try:
        root = inventory.root.resolve()
        resolved = source.resolve()
    except (OSError, RuntimeError) as error:
        raise _fail("unsafe_source_path", "unable to resolve local export file") from error
    if (
        _is_link_or_reparse(source)
        or not resolved.is_relative_to(root)
        or not source.is_file()
    ):
        raise _fail("unsafe_source_path", "local export file is not a safe regular file")
    try:
        data = source.read_bytes()
    except OSError as error:
        raise _fail("unreadable_source", "unable to read local export file") from error
    if len(data) != record.size or hashlib.sha256(data).hexdigest() != record.sha256:
        raise _fail("changed_source", "local export file changed after inventory")
    return data


def _sanitize_asset_name(source_ref: str) -> str:
    source = PurePosixPath(source_ref)
    extension = source.suffix.lower()
    normalized = unicodedata.normalize("NFKD", source.stem)
    ascii_stem = normalized.encode("ascii", "ignore").decode("ascii").lower()
    stem = re.sub(r"[^a-z0-9]+", "-", ascii_stem).strip("-") or "image"
    return f"{stem[:80]}{extension}"


def _yaml_scalar(value: str) -> str:
    rendered = yaml.safe_dump(
        value, allow_unicode=True, default_flow_style=True, width=1_000_000
    ).strip()
    return rendered.removesuffix("\n...").removesuffix("...").rstrip()


def _front_matter(plan: ImportArticlePlan) -> str:
    if (
        not plan.title.strip()
        or plan.published_at is None
        or plan.kind is None
        or plan.summary is None
        or not plan.summary.strip()
        or not plan.tags
    ):
        raise _fail(
            "missing_metadata", "selected candidate is missing reviewed public metadata"
        )
    lines = [
        "---",
        f"title: {_yaml_scalar(plan.title.strip())}",
        f"slug: {plan.slug}",
        f"published_at: {plan.published_at}",
        f"kind: {plan.kind}",
        "public: true",
        f"summary: {_yaml_scalar(plan.summary.strip())}",
        "tags:",
        *(f"  - {_yaml_scalar(tag)}" for tag in plan.tags),
        "source: notion",
        "---",
    ]
    return "\n".join(lines) + "\n"


def _write_bundle(
    destination_root: str | Path,
    plan: ImportArticlePlan,
    index_text: str,
    assets: Mapping[str, bytes],
) -> Path:
    try:
        destination = private_import_path(destination_root)
        bundle_root = private_import_path(destination / plan.slug)
    except NotionImportError as error:
        raise _fail("unsafe_destination", "preview bundle path is unsafe") from error
    if bundle_root.exists():
        raise _fail("unsafe_destination", "preview bundle already exists")
    try:
        bundle_root.mkdir(parents=True)
        (bundle_root / "index.md").write_text(
            index_text, encoding="utf-8", newline="\n"
        )
        if assets:
            assets_root = bundle_root / "assets"
            assets_root.mkdir()
            for name, data in sorted(assets.items()):
                (assets_root / name).write_bytes(data)
    except OSError as error:
        if bundle_root.exists() and bundle_root.parent == destination:
            shutil.rmtree(bundle_root, ignore_errors=True)
        raise _fail("write_failed", "unable to write converted preview bundle") from error
    return bundle_root


def convert_notion_page(
    plan_article: ImportArticlePlan,
    inventory: ExportInventory,
    selected_routes: Mapping[str, str],
    destination_root: str | Path,
) -> ConvertedBundle:
    """Convert one explicitly selected Notion page into a private writing bundle."""
    source_record = inventory.files.get(plan_article.source_ref)
    if source_record is None or plan_article.source_ref not in inventory.markdown_paths:
        raise _fail("missing_source", "selected Markdown candidate is missing from export")
    source_bytes = _read_inventory_file(source_record, inventory)
    try:
        body = source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _fail(
            "unreadable_source", "selected Markdown candidate is not valid UTF-8"
        ) from error
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = _remove_matching_h1(body, plan_article.detected_title)
    body, protected = _protect_code(body)
    body = _convert_asides(body)
    issues: list[ImportIssue] = []
    if _AUTHOR_HTML.search(body):
        issues.append(
            ImportIssue(
                plan_article.source_ref,
                "invalid_notion_html",
                "Unrecognized HTML may lose content in preview",
            )
        )

    asset_data: dict[str, bytes] = {}
    content_names: dict[str, str] = {}
    name_hashes: dict[str, str] = {}
    route_keys = {portable_collision_key(key): value for key, value in selected_routes.items()}

    def rewrite(match: re.Match[str]) -> str:
        image = bool(match.group(1))
        label = match.group(2)
        raw_target = match.group(3).strip()
        split = urlsplit(raw_target)
        if image and (split.scheme or split.netloc):
            raise _fail("remote_image", "remote images are not supported")
        if not image and (split.scheme in {"http", "https", "mailto"} or split.netloc):
            return match.group(0)
        if split.scheme:
            return match.group(0) if not image else match.group(0)
        if split.query:
            raise _fail("unsafe_source_path", "local references may not contain queries")
        if not split.path:
            if image:
                raise _fail("missing_image", "image reference has no local path")
            return match.group(0)
        resolved_ref = _resolve_reference(plan_article.source_ref, split.path)
        inventory_ref = _inventory_name(inventory, resolved_ref)
        if image:
            if inventory_ref is None:
                raise _fail("missing_image", "local image is missing from export")
            if PurePosixPath(inventory_ref).suffix.lower() not in SUPPORTED_ASSET_EXTENSIONS:
                raise _fail("unsupported_image", "local image extension is not supported")
            data = _read_inventory_file(inventory.files[inventory_ref], inventory)
            digest = hashlib.sha256(data).hexdigest()
            destination_name = content_names.get(digest)
            if destination_name is None:
                destination_name = _sanitize_asset_name(inventory_ref)
                prior = name_hashes.get(destination_name)
                if prior is not None and prior != digest:
                    stem = PurePosixPath(destination_name).stem
                    suffix = PurePosixPath(destination_name).suffix
                    destination_name = f"{stem}-{digest[:8]}{suffix}"
                    if destination_name in name_hashes and name_hashes[destination_name] != digest:
                        raise _fail("asset_collision", "local images have an unsafe name collision")
                content_names[digest] = destination_name
                name_hashes[destination_name] = digest
                asset_data[destination_name] = data
            return f"![{label}](assets/{destination_name})"
        if inventory_ref is None or PurePosixPath(inventory_ref).suffix.lower() != ".md":
            if inventory_ref is not None:
                raise _fail("unsupported_attachment", "local attachments are not supported")
            raise _fail("unresolved_page_link", "local page link cannot be resolved")
        route = selected_routes.get(inventory_ref) or route_keys.get(
            portable_collision_key(inventory_ref)
        )
        if route is None:
            raise _fail("unresolved_page_link", "local page link targets an unselected page")
        fragment = _safe_fragment(split.fragment)
        suffix = f"#{fragment}" if fragment else ""
        return f"[{label}]({route}.html{suffix})"

    body = _MARKDOWN_REFERENCE.sub(rewrite, body)
    body = _restore_code(body, protected)
    index_text = _front_matter(plan_article) + body
    bundle_root = _write_bundle(
        destination_root, plan_article, index_text, asset_data
    )
    return ConvertedBundle(bundle_root, tuple(issues))
