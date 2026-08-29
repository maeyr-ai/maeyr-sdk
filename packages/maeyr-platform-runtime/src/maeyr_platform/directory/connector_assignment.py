"""Pure connector-key validation and profile-field lookup policies."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def normalize_phone(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw[1:])
        return f"+{digits}" if digits else ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    return f"+{digits}" if digits else ""


def validate_connector_primary_key(
    value: Any,
    validation_type: str,
    validation_pattern: Optional[str] = None,
) -> tuple[bool, str, Optional[str]]:
    """Validate a connector primary key and return its canonical form."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return False, "", "Primary key is empty"

    val_str = str(value).strip()
    vtype = (validation_type or "").strip().lower()
    if vtype == "phone":
        normalized = normalize_phone(val_str)
        if len(re.sub(r"\D", "", normalized)) < 7:
            return False, "", "Invalid phone number format"
        return True, normalized, None
    if vtype == "email":
        normalized = val_str.lower()
        if not EMAIL_PATTERN.fullmatch(normalized):
            return False, "", "Invalid email address format"
        return True, normalized, None
    if vtype == "regex" and validation_pattern:
        try:
            if not re.compile(validation_pattern).fullmatch(val_str):
                return False, "", f"Value does not match regex pattern: {validation_pattern}"
            return True, val_str, None
        except re.error as exc:
            return False, "", f"Invalid validation regex: {exc}"
    return True, val_str, None


def get_nested_value(profile: Dict[str, Any], path: str) -> Any:
    """Extract a dotted value from either a profile root or ``profile.*`` path."""
    if not path:
        return None
    normalized_path = path.strip()
    if normalized_path.startswith("profile."):
        normalized_path = normalized_path[8:]
    current: Any = profile
    for part in normalized_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


__all__ = ["get_nested_value", "normalize_phone", "validate_connector_primary_key"]
