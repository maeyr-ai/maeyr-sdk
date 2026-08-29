"""CSV import and export primitives shared by Directory Sync and Volt."""

from __future__ import annotations

import csv
import io
from typing import Any


def parse_project_users_csv(
    csv_text: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse project-user CSV text into normalized row dictionaries."""

    errors: list[str] = []
    if not (csv_text or "").strip():
        return [], ["empty CSV"]
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        return [], ["CSV missing header row"]
    rows: list[dict[str, Any]] = []
    for row in reader:
        cleaned = {
            (key or "").strip(): (value or "").strip()
            for key, value in row.items()
            if key and (key or "").strip()
        }
        if not any(cleaned.values()):
            continue
        rows.append(cleaned)
    if not rows:
        errors.append("no data rows found")
    return rows, errors


def format_project_users_csv(
    rows: list[dict[str, Any]],
    *,
    fieldnames: list[str],
) -> str:
    """Serialize project-user rows using the established flat CSV contract."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        profile = row.get("profile") or {}
        output = {"customer_user_id": row.get("customer_user_id", ""), **profile}
        output["enabled"] = row.get("enabled", True)
        writer.writerow({key: output.get(key, "") for key in fieldnames})
    return buffer.getvalue()


__all__ = ["format_project_users_csv", "parse_project_users_csv"]
