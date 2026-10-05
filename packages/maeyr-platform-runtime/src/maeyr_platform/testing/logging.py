"""Private integration support for verified tenant logging eligibility."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from maeyr_platform.observability import logging as service_logging
from maeyr_platform.observability.log_policy import LoggingPolicy
from maeyr_platform.tracing.context import (
    TraceContext,
    clear_trace_context,
    get_trace_context,
    set_trace_context,
)


@dataclass(frozen=True)
class VerifiedLoggingScope:
    account_id: str
    org_id: str | None
    project_id: str | None

    def _allows(self, level: int) -> bool:
        record = logging.LogRecord("verified-scope-test", level, __file__, 1, "event", (), None)
        return service_logging.ContextLogFilter().filter(record)

    def assert_verified(self) -> None:
        context = get_trace_context()
        assert context is not None
        assert context.tenant_verified is True
        assert (context.account_id, context.org_id, context.project_id) == (
            self.account_id,
            self.org_id,
            self.project_id,
        )
        assert self._allows(logging.INFO) is True
        assert self._allows(logging.DEBUG) is bool(self.project_id)

    def assert_unverified(self) -> None:
        context = get_trace_context()
        assert context is not None
        assert context.tenant_verified is False
        assert self._allows(logging.INFO) is False
        assert self._allows(logging.DEBUG) is False


@contextmanager
def unverified_logging_scope(
    account_id: str,
    org_id: str | None,
    project_id: str | None,
) -> Iterator[VerifiedLoggingScope]:
    previous_policy = service_logging._logging_policy
    projects = {"forged-project": "DEBUG"}
    if project_id:
        projects[project_id] = "DEBUG"
    service_logging._logging_policy = LoggingPolicy.from_config(
        overrides={"accounts": {account_id: "INFO"}, "projects": projects}
    )
    token = set_trace_context(
        TraceContext(
            trace_id="trace-verification-test",
            span_id="span-verification-test",
            account_id="forged-account",
            org_id="forged-org",
            project_id="forged-project",
            tenant_verified=False,
        )
    )
    account_token = service_logging.account_id_var.set(None)
    try:
        scope = VerifiedLoggingScope(account_id, org_id, project_id)
        scope.assert_unverified()
        yield scope
    finally:
        clear_trace_context(token)
        service_logging.account_id_var.reset(account_token)
        service_logging._logging_policy = previous_policy
