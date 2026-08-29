"""Canonical logo URL and inline SVG validation for widget appearance."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

_LOGO_URL_RE = re.compile(
    r"^https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::\d{1,5})?"
    r"(?:/[^\s?#]*)?(?:\?[^\s#]*)?(?:#[^\s]*)?$",
    re.I,
)
_FORBIDDEN_SVG = re.compile(
    r"(<script|javascript:|data:text/html|on\w+\s*=|<foreignobject|<iframe|<embed|<object)",
    re.I,
)
_EVENT_ATTR_RE = re.compile(r"\s(on\w+)\s*=\s*([\"']).*?\2", re.I)


def normalize_logo_url(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    if len(raw) > 2048:
        raise ValueError("logo_url too long")
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        raise ValueError("logo_url must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("logo_url must not contain credentials")
    if not _LOGO_URL_RE.match(raw):
        raise ValueError("logo_url must be a valid HTTPS URL")
    return raw


def sanitize_logo_svg(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    if len(raw) > 12000:
        raise ValueError("logo_svg too long")
    if not re.match(r"^\s*<svg[\s>]", raw, re.I):
        raise ValueError("logo_svg must be an SVG element")
    if _FORBIDDEN_SVG.search(raw):
        raise ValueError("logo_svg contains disallowed content")
    cleaned = _EVENT_ATTR_RE.sub("", raw)
    if _FORBIDDEN_SVG.search(cleaned):
        raise ValueError("logo_svg contains disallowed content")
    if len(cleaned) > 8192:
        raise ValueError("logo_svg too large after sanitization")
    return cleaned
