"""Strict, incremental decoding for JSON Server-Sent Events."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, cast

from maeyr.client.errors import MaeyrStreamError


class JsonSseDecoder:
    """Decode SSE framing without silently discarding corrupt application events."""

    def __init__(self) -> None:
        self._data: list[str] = []
        self.finished = False

    def feed(self, line: str) -> Optional[Dict[str, Any]]:
        if self.finished:
            return None
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None

        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        return None

    def finish(self) -> Optional[Dict[str, Any]]:
        """Dispatch an unterminated final event when the connection closes."""
        if self.finished:
            return None
        return self._dispatch()

    def _dispatch(self) -> Optional[Dict[str, Any]]:
        if not self._data:
            return None
        payload = "\n".join(self._data)
        self._data.clear()
        if payload.strip() == "[DONE]":
            self.finished = True
            return None
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MaeyrStreamError("Stream returned malformed JSON event") from exc
        if not isinstance(value, dict):
            raise MaeyrStreamError("Stream returned a non-object JSON event")
        return cast(Dict[str, Any], value)
