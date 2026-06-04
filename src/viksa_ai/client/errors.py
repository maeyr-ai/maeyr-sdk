from typing import Any, Optional


class ViksaApiError(Exception):
    """Raised when the Viksa API returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Any = None,
        service: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.service = service
