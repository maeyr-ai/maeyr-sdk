"""Validate project user profile fields against schema type + constraints."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def clean_allowed_values(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        return [p for p in parts if p]
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return []


def match_allowed_value(value: Any, allowed: List[str]) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    lowered = raw.lower()
    for candidate in allowed:
        if candidate.lower() == lowered:
            return candidate
    return None


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_phone(value: str) -> str:
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


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in ("true", "1", "yes", "y", "on"):
        return True
    if raw in ("false", "0", "no", "n", "off"):
        return False
    return None


def _parse_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _compile_pattern(pattern: str) -> Optional[re.Pattern[str]]:
    text = (pattern or "").strip()
    if not text:
        return None
    try:
        return re.compile(text)
    except re.error:
        return None


def validate_schema_field_defs(fields: List[Dict[str, Any]]) -> List[str]:
    """Return schema-level errors (invalid regex, boolean constraints, etc.)."""
    errors: List[str] = []
    for field_def in fields or []:
        if not isinstance(field_def, dict):
            continue
        name = str(field_def.get("name") or "").strip() or "field"
        ftype = str(field_def.get("type") or "string")
        pattern = str(field_def.get("pattern") or "").strip()
        allowed = clean_allowed_values(field_def.get("allowed_values"))

        if ftype == "boolean":
            if allowed:
                errors.append(f"{name}: boolean fields cannot have allowed values")
            if pattern:
                errors.append(f"{name}: boolean fields cannot have a regex pattern")
            continue

        if pattern and _compile_pattern(pattern) is None:
            errors.append(f"{name}: invalid regex pattern")

        if ftype in {"email", "phone", "number"} and allowed:
            errors.append(
                f"{name}: {ftype} fields use type validation — use regex instead of allowed values"
            )

        if ftype == "number":
            min_value = field_def.get("min_value")
            max_value = field_def.get("max_value")
            if min_value is not None and max_value is not None:
                try:
                    if float(min_value) > float(max_value):
                        errors.append(f"{name}: min_value cannot exceed max_value")
                except (TypeError, ValueError):
                    errors.append(f"{name}: min_value/max_value must be numeric")

    return errors


def _apply_pattern(field_name: str, value: str, pattern: str) -> Optional[str]:
    compiled = _compile_pattern(pattern)
    if not compiled:
        return value
    if not compiled.fullmatch(value):
        return None
    return value


def validate_field_value(
    field_def: Dict[str, Any],
    value: Any,
) -> Tuple[Optional[Any], Optional[str]]:
    """Validate and coerce a single profile field. Returns (coerced_value, error)."""
    name = str(field_def.get("name") or "").strip() or "field"
    ftype = str(field_def.get("type") or "string")
    pattern = str(field_def.get("pattern") or "").strip()
    allowed = clean_allowed_values(field_def.get("allowed_values"))

    empty = value is None or (isinstance(value, str) and not str(value).strip())
    if empty:
        return None, None

    if ftype == "boolean":
        parsed_bool = _parse_bool(value)
        if parsed_bool is None:
            return None, f"{name}: must be true or false"
        return parsed_bool, None

    if ftype == "email":
        normalized = _normalize_email(str(value))
        if not EMAIL_PATTERN.fullmatch(normalized):
            return None, f"{name}: invalid email format"
        if allowed:
            email_canonical = match_allowed_value(normalized, allowed)
            if email_canonical is None:
                return None, f"{name}: must be one of {', '.join(allowed)}"
            normalized = email_canonical
        if pattern:
            if _apply_pattern(name, normalized, pattern) is None:
                return None, f"{name}: does not match pattern"
        return normalized, None

    if ftype == "phone":
        normalized = _normalize_phone(str(value))
        digits = re.sub(r"\D", "", normalized)
        if len(digits) < 7:
            return None, f"{name}: invalid phone number"
        if pattern and _apply_pattern(name, normalized, pattern) is None:
            return None, f"{name}: does not match pattern"
        return normalized, None

    if ftype == "number":
        parsed_number = _parse_number(value)
        if parsed_number is None:
            return None, f"{name}: must be a number"
        min_value = field_def.get("min_value")
        max_value = field_def.get("max_value")
        if min_value is not None:
            try:
                if parsed_number < float(min_value):
                    return None, f"{name}: must be >= {min_value}"
            except (TypeError, ValueError):
                pass
        if max_value is not None:
            try:
                if parsed_number > float(max_value):
                    return None, f"{name}: must be <= {max_value}"
            except (TypeError, ValueError):
                pass
        numeric_canonical: Any = int(parsed_number) if parsed_number.is_integer() else parsed_number
        canonical_str = str(numeric_canonical)
        if pattern and _apply_pattern(name, canonical_str, pattern) is None:
            return None, f"{name}: does not match pattern"
        return numeric_canonical, None

    # string (default)
    text = str(value).strip()
    if allowed:
        string_canonical = match_allowed_value(text, allowed)
        if string_canonical is None:
            return None, f"{name}: must be one of {', '.join(allowed)}"
        text = string_canonical
    elif pattern:
        if _apply_pattern(name, text, pattern) is None:
            return None, f"{name}: does not match pattern"
    return text, None


def validate_profile_against_schema(
    schema: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
    *,
    remote: bool = False,
) -> Tuple[Dict[str, Any], bool, List[str]]:
    if not schema:
        return dict(profile), True, []

    out = dict(profile)
    errors: List[str] = []
    valid = True
    field_defs = {
        str(f.get("name")): f
        for f in (schema.get("fields") or [])
        if isinstance(f, dict) and f.get("name")
    }

    for name, field_def in field_defs.items():
        required = bool(field_def.get("required"))
        raw_val = out.get(name)
        has_val = raw_val is not None and not (
            isinstance(raw_val, str) and not str(raw_val).strip()
        )

        if not has_val:
            if required:
                errors.append(f"{name}: required")
                valid = False
            continue

        coerced, err = validate_field_value(field_def, raw_val)
        if err:
            out.pop(name, None)
            if required:
                errors.append(err)
                valid = False
            elif remote:
                errors.append(f"{err} — field cleared")
            else:
                errors.append(f"{err} — field cleared")
            continue
        if coerced is None:
            out.pop(name, None)
            continue
        out[name] = coerced

    return out, valid, errors


def count_valid_users_query(scope_pf: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **scope_pf,
        "$or": [{"profile_valid": True}, {"profile_valid": {"$exists": False}}],
    }


def count_invalid_users_query(scope_pf: Dict[str, Any]) -> Dict[str, Any]:
    return {**scope_pf, "profile_valid": False}
