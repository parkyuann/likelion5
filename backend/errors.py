"""백엔드 경계에서 사용하는 구조화 오류."""

from __future__ import annotations

from typing import Any


class BackendError(RuntimeError):
    """프론트엔드에 안전하게 노출할 수 있는 오류 코드와 HTTP 상태를 보존한다."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_code": self.code,
            "message": self.message,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload
