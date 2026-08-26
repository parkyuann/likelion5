"""환경변수로 설정되는 KOSIS Streamable HTTP MCP 클라이언트."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from backend.errors import BackendError


@dataclass(frozen=True)
class KosisMcpConfig:
    url: str
    tool_name: str = "query_kosis"
    question_argument: str = "question"
    protocol_version: str = "2025-06-18"
    timeout_seconds: float = 30.0
    bearer_token: str | None = None

    @classmethod
    def from_env(cls) -> "KosisMcpConfig":
        url = os.getenv("KOSIS_MCP_URL", "").strip()
        if not url:
            raise BackendError(
                "KOSIS_MCP_NOT_CONFIGURED",
                "KOSIS MCP 서버 주소가 설정되지 않았습니다.",
                status_code=503,
            )
        return cls(
            url=url,
            tool_name=os.getenv("KOSIS_MCP_TOOL", "query_kosis").strip() or "query_kosis",
            question_argument=(
                os.getenv("KOSIS_MCP_QUESTION_ARGUMENT", "question").strip() or "question"
            ),
            protocol_version=(
                os.getenv("KOSIS_MCP_PROTOCOL_VERSION", "2025-06-18").strip()
                or "2025-06-18"
            ),
            timeout_seconds=float(os.getenv("KOSIS_MCP_TIMEOUT_SECONDS", "30")),
            bearer_token=os.getenv("KOSIS_MCP_TOKEN") or None,
        )


class KosisMcpClient:
    """MCP 초기화, 도구 확인, 단일 KOSIS 질의 호출을 수행한다."""

    def __init__(
        self,
        config: KosisMcpConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if config.bearer_token:
            headers["Authorization"] = f"Bearer {config.bearer_token}"
        self.client = client or httpx.Client(
            timeout=config.timeout_seconds,
            headers=headers,
            follow_redirects=False,
            trust_env=False,
        )
        self._session_id: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "KosisMcpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Mcp-Session-Id": self._session_id} if self._session_id else {}

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise BackendError(
                "KOSIS_MCP_HTTP_ERROR",
                f"KOSIS MCP가 HTTP {response.status_code}를 반환했습니다.",
                status_code=502,
            )
        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        try:
            if "text/event-stream" in content_type:
                data_lines = [
                    line[5:].strip()
                    for line in response.text.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    raise ValueError("SSE data 없음")
                return json.loads(data_lines[-1])
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise BackendError(
                "KOSIS_MCP_INVALID_RESPONSE",
                "KOSIS MCP 응답을 JSON으로 해석할 수 없습니다.",
                status_code=502,
            ) from exc

    def _post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post(
                self.config.url,
                headers=self._headers(),
                json=dict(payload),
            )
        except httpx.TimeoutException as exc:
            raise BackendError(
                "KOSIS_MCP_TIMEOUT",
                "KOSIS MCP 응답 시간이 초과되었습니다.",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                "KOSIS_MCP_CONNECTION_ERROR",
                "KOSIS MCP 서버에 연결할 수 없습니다.",
                status_code=502,
            ) from exc
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        return self._decode_response(response)

    @staticmethod
    def _result(payload: Mapping[str, Any], operation: str) -> dict[str, Any]:
        error = payload.get("error")
        if error:
            raise BackendError(
                "KOSIS_MCP_TOOL_ERROR",
                f"KOSIS MCP {operation} 호출이 실패했습니다.",
                status_code=502,
                detail={"mcp_error": error},
            )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise BackendError(
                "KOSIS_MCP_INVALID_RESPONSE",
                f"KOSIS MCP {operation} 결과가 객체가 아닙니다.",
                status_code=502,
            )
        return dict(result)

    def initialize(self) -> None:
        payload = self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": self.config.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "likelion5-backend", "version": "0.1.0"},
            },
        })
        self._result(payload, "initialize")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def query(self, question: str) -> dict[str, Any]:
        self.initialize()
        listed = self._result(
            self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            "tools/list",
        )
        names = {
            str(tool.get("name"))
            for tool in listed.get("tools", [])
            if isinstance(tool, Mapping)
        }
        if self.config.tool_name not in names:
            raise BackendError(
                "KOSIS_MCP_TOOL_NOT_FOUND",
                f"설정한 MCP 도구를 찾을 수 없습니다: {self.config.tool_name}",
                status_code=503,
                detail={"available_tools": sorted(names)},
            )
        result = self._result(
            self._post({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": self.config.tool_name,
                    "arguments": {self.config.question_argument: question},
                },
            }),
            "tools/call",
        )
        if result.get("isError"):
            raise BackendError(
                "KOSIS_MCP_TOOL_ERROR",
                "KOSIS MCP 도구가 오류 결과를 반환했습니다.",
                status_code=502,
            )
        return result


def content_text(result: Mapping[str, Any]) -> str:
    """MCP content 배열의 텍스트 블록을 사용자 답변 문자열로 합친다."""
    texts = [
        str(block.get("text", ""))
        for block in result.get("content", [])
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(text for text in texts if text).strip()
