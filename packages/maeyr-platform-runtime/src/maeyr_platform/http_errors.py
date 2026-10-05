"""Safe field validation responses for platform HTTP applications."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from itertools import islice

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

_MAX_ERRORS = 64
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_MESSAGES = {
    "missing": "Field is required",
    "extra_forbidden": "Remove unsupported fields",
    "string_type": "Enter a text value",
    "string_unicode": "Enter valid Unicode text",
    "string_pattern_mismatch": "Value does not match this field's required format",
    "bool_type": "Choose true or false",
    "bool_parsing": "Choose true or false",
    "int_type": "Enter a whole number",
    "int_parsing": "Enter a whole number",
    "int_from_float": "Enter a whole number",
    "int_parsing_size": "Number is too large",
    "float_type": "Enter a number",
    "float_parsing": "Enter a number",
    "finite_number": "Enter a finite number",
    "decimal_type": "Enter a decimal number",
    "decimal_parsing": "Enter a decimal number",
    "decimal_max_digits": "Number has too many digits",
    "decimal_max_places": "Number has too many decimal places",
    "decimal_whole_digits": "Number has too many whole-number digits",
    "dict_type": "Enter an object",
    "mapping_type": "Enter a valid object",
    "model_type": "Enter a valid object",
    "model_attributes_type": "Enter a valid object",
    "list_type": "Enter a list",
    "tuple_type": "Enter a list",
    "set_type": "Enter a list of unique values",
    "frozen_set_type": "Enter a list of unique values",
    "enum": "Choose one of the supported options",
    "literal_error": "Choose one of the supported options",
    "union_tag_invalid": "Choose a supported option for this object",
    "union_tag_not_found": "Specify the required option for this object",
    "none_required": "Value must be empty",
    "date_type": "Enter a date",
    "date_parsing": "Enter a valid date",
    "date_from_datetime_parsing": "Enter a valid date",
    "date_from_datetime_inexact": "Enter a date without a time",
    "date_past": "Enter a date in the past",
    "date_future": "Enter a date in the future",
    "datetime_type": "Enter a date and time",
    "datetime_parsing": "Enter a valid date and time",
    "datetime_from_date_parsing": "Enter a valid date and time",
    "datetime_past": "Enter a date and time in the past",
    "datetime_future": "Enter a date and time in the future",
    "timezone_aware": "Include a timezone",
    "timezone_naive": "Enter a date and time without a timezone",
    "time_type": "Enter a time",
    "time_parsing": "Enter a valid time",
    "time_delta_type": "Enter a duration",
    "time_delta_parsing": "Enter a valid duration",
    "uuid_type": "Enter an identifier",
    "uuid_parsing": "Enter a valid identifier",
    "uuid_version": "Enter an identifier of the required version",
    "url_type": "Enter a URL",
    "url_parsing": "Enter a valid URL",
    "url_scheme": "Use a supported URL scheme",
    "url_syntax_violation": "Enter a valid URL",
    "url_too_long": "URL is too long",
    "bytes_type": "Enter a bytes value",
    "bytes_invalid_encoding": "Enter a value with valid encoding",
    "json_type": "Enter JSON text",
    "json_invalid": "Enter valid JSON",
    "recursion_loop": "Object contains a circular reference",
    # Custom validator messages can contain submitted values or exceptions.
    "value_error": "Value does not meet this field's requirements",
    "assertion_error": "Value does not meet this field's requirements",
    "invalid_key": "Use supported field names",
    "invalid_value": "Value is invalid",
    "invalid_cron_expression": "Enter a cron expression with a valid future execution time",
}
_BOUND_MESSAGES = {
    "greater_than": ("gt", "Value must be greater than {}"),
    "greater_than_equal": ("ge", "Value must be at least {}"),
    "less_than": ("lt", "Value must be less than {}"),
    "less_than_equal": ("le", "Value must be at most {}"),
    "multiple_of": ("multiple_of", "Value must be a multiple of {}"),
    "string_too_short": ("min_length", "Use at least {} characters"),
    "string_too_long": ("max_length", "Use at most {} characters"),
    "bytes_too_short": ("min_length", "Use at least {} bytes"),
    "bytes_too_long": ("max_length", "Use at most {} bytes"),
    "too_short": ("min_length", "Include at least {} items"),
    "too_long": ("max_length", "Include at most {} items"),
}
_SENSITIVE_FIELDS = (
    "password",
    "credential",
    "secret",
    "token",
    "api_key",
    "private_key",
    "connection_string",
)
_KNOWN_TYPES = frozenset(_MESSAGES) | frozenset(_BOUND_MESSAGES)


def _safe_location(raw: object, *, unknown_field: bool) -> list[str | int]:
    location: list[str | int] = []
    if isinstance(raw, (list, tuple)):
        for item in raw[:12]:
            if isinstance(item, str):
                # Variable map keys and unusual labels are not public diagnostics.
                field = item if _FIELD_NAME.fullmatch(item) else "field"
                location.append(field)
                if any(name in field.lower() for name in _SENSITIVE_FIELDS):
                    break
            elif type(item) is int and item >= 0:
                location.append(item)
            else:
                location.append("field")
    if unknown_field and location:
        # The rejected extra field itself is caller data, not a schema field.
        location.pop()
    return location or ["body"]


def sanitized_validation_detail(errors: Iterable[object]) -> list[dict[str, object]]:
    """Render fresh bounded errors without reflecting input, context, or raw messages."""
    details: list[dict[str, object]] = []
    for error in islice(errors, _MAX_ERRORS):
        if not isinstance(error, Mapping):
            continue
        kind = error.get("type")
        if not isinstance(kind, str) or kind not in _KNOWN_TYPES:
            kind = "invalid_value"
        message = _MESSAGES.get(kind, "Value does not meet this field's requirements")
        if kind in _BOUND_MESSAGES:
            key, template = _BOUND_MESSAGES[kind]
            context = error.get("ctx")
            bound = context.get(key) if isinstance(context, Mapping) else None
            if type(bound) is int and abs(bound) <= 10**12:
                message = template.format(bound)
            elif type(bound) is float and math.isfinite(bound) and abs(bound) <= 10**12:
                message = template.format(bound)
        details.append(
            {
                "loc": _safe_location(error.get("loc"), unknown_field=kind == "extra_forbidden"),
                "msg": message,
                "type": kind,
            }
        )
    return details or [{"loc": ["body"], "msg": "Request is invalid", "type": "invalid_value"}]


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Keep the standard 422 field-error envelope without sensitive reflection."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    return JSONResponse(
        status_code=422,
        content={"detail": sanitized_validation_detail(exc.errors())},
        headers={"Cache-Control": "no-store"},
    )


def install_validation_error_handler(app: FastAPI) -> None:
    """Register once at application construction; business validation is unchanged."""
    app.add_exception_handler(RequestValidationError, validation_exception_handler)


__all__ = [
    "install_validation_error_handler",
    "sanitized_validation_detail",
    "validation_exception_handler",
]
