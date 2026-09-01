"""Private content-addressed cache for validated WeRead summaries."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from writings.importers.durability import durable_atomic_write
from writings.importers.models import (
    WEREAD_NAMESPACE,
    WeReadImportError,
    canonical_private_root,
    private_import_path,
)

from .models import BookNotes, SummaryCacheKey, SummaryResult
from .prompts import PROMPT_VERSION, TRANSPORT_VERSION


_CACHE_VERSION = 1
_MAX_CACHE_BYTES = 256 * 1024


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _selected_content(book: BookNotes) -> dict[str, object]:
    return {
        "title": book.title,
        "author": book.author,
        "sections": [
            {
                "name": section.name,
                "items": [[chapter, text] for chapter, text in section.items],
            }
            for section in book.sections
        ],
    }


def _key_inputs(key: SummaryCacheKey) -> dict[str, str]:
    return {
        "source_fingerprint": key.source_fingerprint,
        "content_fingerprint": key.content_fingerprint,
        "prompt_version": key.prompt_version,
        "model": key.model,
        "transport_version": key.transport_version,
    }


def _result_object(result: SummaryResult) -> dict[str, object]:
    return {
        "one_sentence": result.one_sentence,
        "key_ideas": list(result.key_ideas),
        "reflections": list(result.reflections),
        "questions": list(result.questions),
    }


class SummaryCache:
    """Read and durably replace cache envelopes below the canonical private root."""

    def __init__(self) -> None:
        root = canonical_private_root(WEREAD_NAMESPACE) / "cache"
        self.root = private_import_path(root, WEREAD_NAMESPACE)

    def key_for(
        self,
        book: BookNotes,
        model: str,
        *,
        prompt_version: str = PROMPT_VERSION,
        transport_version: str = TRANSPORT_VERSION,
    ) -> SummaryCacheKey:
        content_fingerprint = _sha256(_canonical_json(_selected_content(book)))
        inputs = {
            "source_fingerprint": book.source_fingerprint,
            "content_fingerprint": content_fingerprint,
            "prompt_version": prompt_version,
            "model": model,
            "transport_version": transport_version,
        }
        return SummaryCacheKey(**inputs)

    def path_for(self, key: SummaryCacheKey) -> Path:
        if len(key.digest) != 64 or any(
            character not in "0123456789abcdef" for character in key.digest
        ):
            raise WeReadImportError("invalid_cache_key", "summary cache key is invalid")
        path = self.root / key.digest[:2] / f"{key.digest}.json"
        return private_import_path(path, WEREAD_NAMESPACE)

    def load(self, key: SummaryCacheKey) -> SummaryResult | None:
        path = self.path_for(key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if len(raw) > _MAX_CACHE_BYTES:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(envelope, dict) or set(envelope) != {
            "version",
            "key_inputs",
            "result",
            "checksum",
        }:
            return None
        body = {
            "version": envelope["version"],
            "key_inputs": envelope["key_inputs"],
            "result": envelope["result"],
        }
        checksum = envelope["checksum"]
        if (
            envelope["version"] != _CACHE_VERSION
            or envelope["key_inputs"] != _key_inputs(key)
            or not isinstance(checksum, str)
            or not hmac.compare_digest(checksum, _sha256(_canonical_json(body)))
            or raw != _canonical_json(envelope) + b"\n"
        ):
            return None
        result = envelope["result"]
        if not isinstance(result, dict) or set(result) != {
            "one_sentence",
            "key_ideas",
            "reflections",
            "questions",
        }:
            return None
        try:
            return SummaryResult(
                one_sentence=result["one_sentence"],
                key_ideas=result["key_ideas"],
                reflections=result["reflections"],
                questions=result["questions"],
            )
        except (TypeError, ValueError):
            return None

    def store(self, key: SummaryCacheKey, result: SummaryResult) -> Path:
        body: dict[str, Any] = {
            "version": _CACHE_VERSION,
            "key_inputs": _key_inputs(key),
            "result": _result_object(result),
        }
        envelope = {**body, "checksum": _sha256(_canonical_json(body))}
        data = _canonical_json(envelope) + b"\n"
        path = self.path_for(key)
        try:
            supported = durable_atomic_write(path, data, "weread-summary-cache")
        except OSError:
            raise WeReadImportError(
                "cache_write_failed", "unable to write the private summary cache"
            ) from None
        if not supported:
            raise WeReadImportError(
                "cache_write_failed", "summary cache durability is unavailable"
            )
        return path
