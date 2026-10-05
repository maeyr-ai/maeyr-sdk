"""Validation responses keep field guidance without echoing confidential input."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from maeyr_platform.http_errors import (
    install_validation_error_handler,
    sanitized_validation_detail,
)

SECRET = "private-credential"


class SensitiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=24)
    scope: str
    count: int = Field(ge=1)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        if value != "read":
            raise ValueError(f"Unknown scope {value}: provider token {SECRET}")
        return value


def _app() -> FastAPI:
    app = FastAPI()
    install_validation_error_handler(app)

    @app.post("/sensitive")
    async def sensitive(payload: SensitiveRequest) -> dict[str, int]:
        return {"count": payload.count}

    return app


@pytest.mark.parametrize(
    ("payload", "field", "message"),
    [
        (
            {"password": SECRET, "scope": "read", "count": 1},
            "password",
            "Use at least 24 characters",
        ),
        ({"password": SECRET * 2, "scope": SECRET, "count": 1}, "scope", "requirements"),
        ({"password": SECRET * 2, "scope": "read", "count": SECRET}, "count", "whole number"),
        ({"password": SECRET * 2, "scope": "read", "count": 0}, "count", "at least 1"),
    ],
)
async def test_real_asgi_validation_is_readable_without_reflected_input(
    payload: dict[str, Any], field: str, message: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cast(Any, _app())), base_url="https://test"
    ) as client:
        response = await client.post("/sensitive", json=payload)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text
    errors = response.json()["detail"]
    error = next(item for item in errors if field in item["loc"])
    assert message in error["msg"]
    assert set(error) == {"loc", "msg", "type"}


async def test_missing_field_and_unknown_field_have_safe_diagnostics() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cast(Any, _app())), base_url="https://test"
    ) as client:
        response = await client.post(
            "/sensitive", json={"scope": "read", "count": 1, SECRET: SECRET}
        )
    assert response.status_code == 422
    assert SECRET not in response.text
    assert {item["msg"] for item in response.json()["detail"]} == {
        "Field is required",
        "Remove unsupported fields",
    }


async def test_valid_requests_still_reach_the_endpoint() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cast(Any, _app())), base_url="https://test"
    ) as client:
        response = await client.post(
            "/sensitive", json={"password": SECRET * 2, "scope": "read", "count": 2}
        )
    assert response.status_code == 200
    assert response.json() == {"count": 2}


@pytest.mark.parametrize("kind", ["value_error", "assertion_error", SECRET])
def test_validator_messages_context_and_urls_never_cross_the_boundary(kind: str) -> None:
    errors = sanitized_validation_detail(
        [
            {
                "loc": ["body", "credentials", SECRET],
                "type": kind,
                "msg": f"Invalid {SECRET}",
                "input": {"api_key": SECRET},
                "ctx": {"error": ValueError(SECRET), "expected": SECRET},
                "url": f"https://provider.test/?key={SECRET}",
            }
        ]
    )
    assert SECRET not in json.dumps(errors)
    assert errors[0]["loc"] == ["body", "credentials"]
    assert set(errors[0]) == {"loc", "msg", "type"}


@pytest.mark.parametrize("bound", [SECRET, True, float("nan"), float("inf"), 10**100])
def test_constraint_context_must_be_a_bounded_finite_number(bound: object) -> None:
    detail = sanitized_validation_detail(
        [{"type": "greater_than", "loc": ["body", "count"], "ctx": {"gt": bound}}]
    )[0]
    assert detail["msg"] == "Value does not meet this field's requirements"
    assert SECRET not in json.dumps(detail)


def test_error_count_and_field_paths_are_bounded() -> None:
    raw = [{"type": "missing", "loc": ["body"] + [SECRET * 20] * 40}] * 1_000
    errors = sanitized_validation_detail(raw)
    assert len(errors) == 64
    assert all(isinstance(item["loc"], list) and len(item["loc"]) <= 12 for item in errors)
    assert SECRET not in json.dumps(errors)


def test_empty_or_malformed_validation_errors_still_have_a_message() -> None:
    assert sanitized_validation_detail([]) == [
        {"loc": ["body"], "msg": "Request is invalid", "type": "invalid_value"}
    ]
    assert sanitized_validation_detail([None, SECRET]) == sanitized_validation_detail([])


def test_schedule_validation_has_specific_static_guidance() -> None:
    details = sanitized_validation_detail(
        [
            {
                "type": "invalid_cron_expression",
                "loc": ["body", "cron_expression"],
                "input": SECRET,
                "msg": f"Invalid expression {SECRET}",
            }
        ]
    )
    assert details == [
        {
            "type": "invalid_cron_expression",
            "loc": ["body", "cron_expression"],
            "msg": "Enter a cron expression with a valid future execution time",
        }
    ]
