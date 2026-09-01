"""Proxy-free, redirect-free OpenAI-compatible loopback chat transport."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
from typing import Mapping, Sequence
from urllib.parse import SplitResult, urlsplit

from writings.importers.models import WeReadImportError

from .prompts import MAX_MESSAGE_CHARS, message_character_count


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# Hard UTF-8 cap for the complete OpenAI-compatible request body. The messages
# array is independently capped by MAX_MESSAGE_CHARS before any connection.
MAX_REQUEST_BYTES = 100_000
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _validate_connected_peer(connection: http.client.HTTPConnection) -> None:
    sock = connection.sock
    try:
        peer = sock.getpeername() if sock is not None else None
        host = peer[0] if isinstance(peer, tuple) and peer else None
        address = ipaddress.ip_address(host.split("%", 1)[0]) if isinstance(host, str) else None
    except (OSError, ValueError):
        address = None
    if address is None or not address.is_loopback:
        raise WeReadImportError(
            "model_peer_invalid", "connected model peer is not loopback"
        )


def _validated_base_url(base_url: str) -> SplitResult:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise WeReadImportError(
            "invalid_model_url", "model base URL is invalid"
        ) from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/v1", "/v1/"}
    ):
        raise WeReadImportError(
            "invalid_model_url", "model base URL must be literal loopback HTTP"
        )
    if port is not None and not 1 <= port <= 65535:
        raise WeReadImportError(
            "invalid_model_url", "model base URL has an invalid port"
        )
    return parsed


class LoopbackChatClient:
    """Minimal standard-library client that cannot consult proxy settings."""

    def __init__(self, base_url: str) -> None:
        parsed = _validated_base_url(base_url)
        self._host = parsed.hostname or ""
        self._port = parsed.port or 80

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str,
        timeout: float,
    ) -> str:
        if not isinstance(model, str) or not model.strip():
            raise WeReadImportError("invalid_model_request", "model must be non-empty")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise WeReadImportError("invalid_model_request", "timeout must be positive")
        normalized_messages: list[dict[str, str]] = []
        for message in messages:
            if (
                not isinstance(message, Mapping)
                or set(message) != {"role", "content"}
                or message.get("role") not in {"system", "user", "assistant"}
                or not isinstance(message.get("content"), str)
            ):
                raise WeReadImportError(
                    "invalid_model_request", "chat messages violate the local contract"
                )
            normalized_messages.append(
                {"role": message["role"], "content": message["content"]}
            )
        if message_character_count(normalized_messages) > MAX_MESSAGE_CHARS:
            raise WeReadImportError(
                "model_request_too_large",
                "local model request exceeds the message boundary",
            )
        payload = json.dumps(
            {
                "model": model,
                "messages": normalized_messages,
                "temperature": 0,
                "stream": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise WeReadImportError(
                "model_request_too_large",
                "local model request exceeds the byte boundary",
            )
        connection = http.client.HTTPConnection(
            self._host, self._port, timeout=float(timeout)
        )
        try:
            connection.connect()
            _validate_connected_peer(connection)
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise WeReadImportError(
                    "model_redirect", "local model service returned a redirect"
                )
            if response.status != 200:
                raise WeReadImportError(
                    "model_http_error", "local model service returned a non-success status"
                )
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    parsed_length = int(length)
                except ValueError:
                    raise WeReadImportError(
                        "model_response_invalid", "local model response length is invalid"
                    ) from None
                if parsed_length > MAX_RESPONSE_BYTES:
                    raise WeReadImportError(
                        "model_response_too_large",
                        "local model response exceeds the size boundary",
                    )
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except WeReadImportError:
            raise
        except (TimeoutError, socket.timeout):
            raise WeReadImportError(
                "model_timeout", "local model service timed out"
            ) from None
        except (OSError, http.client.HTTPException):
            raise WeReadImportError(
                "model_unavailable", "local model service is unavailable"
            ) from None
        finally:
            connection.close()
        if len(body) > MAX_RESPONSE_BYTES:
            raise WeReadImportError(
                "model_response_too_large",
                "local model response exceeds the size boundary",
            )
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise WeReadImportError(
                "model_response_invalid_utf8", "local model response is not UTF-8"
            ) from None
        try:
            data = json.loads(decoded)
        except (json.JSONDecodeError, RecursionError):
            raise WeReadImportError(
                "model_response_invalid_json", "local model response is not valid JSON"
            ) from None
        if not isinstance(data, dict):
            raise WeReadImportError(
                "model_response_invalid", "local model response has an invalid shape"
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise WeReadImportError(
                "model_response_invalid", "local model response must contain one choice"
            )
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise WeReadImportError(
                "model_response_invalid", "local model response content must be text"
            )
        return content
