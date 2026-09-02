"""WeRead error adapter for the shared loopback chat transport."""

from __future__ import annotations

import http.client
from typing import Mapping, Sequence
from urllib.parse import SplitResult

from shared.loopback_chat import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    LoopbackChatError,
    LoopbackChatTransport,
    validate_connected_peer,
    validate_loopback_base_url,
)
from writings.importers.models import WeReadImportError

from .prompts import MAX_MESSAGE_CHARS


def _as_weread_error(error: LoopbackChatError) -> WeReadImportError:
    return WeReadImportError(error.code, error.message)


def _validated_base_url(base_url: str) -> SplitResult:
    try:
        return validate_loopback_base_url(base_url)
    except LoopbackChatError as error:
        raise _as_weread_error(error) from None


def _validate_connected_peer(connection: http.client.HTTPConnection) -> None:
    try:
        validate_connected_peer(connection)
    except LoopbackChatError as error:
        raise _as_weread_error(error) from None


class LoopbackChatClient:
    """Preserve the WeRead client interface and stable domain errors."""

    def __init__(self, base_url: str) -> None:
        try:
            self._transport = LoopbackChatTransport(
                base_url,
                max_message_chars=MAX_MESSAGE_CHARS,
                max_request_bytes=MAX_REQUEST_BYTES,
                max_response_bytes=MAX_RESPONSE_BYTES,
            )
        except LoopbackChatError as error:
            raise _as_weread_error(error) from None

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str,
        timeout: float,
    ) -> str:
        try:
            return self._transport.complete(
                messages,
                model=model,
                timeout=timeout,
            )
        except LoopbackChatError as error:
            raise _as_weread_error(error) from None
