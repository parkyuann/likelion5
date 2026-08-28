"""HCX chat-completions client.

팀 인계: prompt와 structured output schema를 HCX에 전송하고 응답을 정규화한다.
주장 판단 로직은 포함하지 않는 인프라 모듈이다.

Infrastructure, not a layer.  It sat inside ``article_claim_pipeline.py``, so
L2 — whose only model call this is — had to import the whole r16i contract to
reach it, and importing any layer pulled in ``requests``.

Moved unchanged.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

try:  # Keep deterministic layers testable without the HTTP client installed.
    import requests
except ImportError:  # pragma: no cover - exercised only in minimal test runtimes
    requests = None  # type: ignore[assignment]


# The HCX-007 documentation advertises a higher generic output ceiling, but
# the structured-output endpoint used by this service app rejects 8,000 with
# HTTP 400 while the same dev request succeeds at 4,000.  This is transport
# compatibility, not a change to the L2 prompt or schema contract.
MAX_COMPLETION_TOKENS = 4000


def call_hcx_json(*, system_prompt: str, user_prompt: str, schema: dict[str, Any], api_key: str,
                  model: str, timeout: int, temperature: float = 0.1,
                  top_p: float = 0.8, seed: int | None = None,
                  max_completion_tokens: int = MAX_COMPLETION_TOKENS) -> tuple[dict[str, Any], dict[str, Any], float]:
    if not api_key:
        raise ValueError("HCX API key is required")
    if requests is None:
        raise RuntimeError("requests package is required only for HCX API calls")
    url = f"https://clovastudio.stream.ntruss.com/v3/chat-completions/{model.upper()}"
    body = {
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "temperature": temperature, "topP": top_p, "topK": 0,
        "repetitionPenalty": 1.1,
        # L2 chunks at 15 sentences so the response remains within the actual
        # structured-output limit observed for this service app.
        "maxCompletionTokens": max_completion_tokens,
        "thinking": {"effort": "none"},
        "responseFormat": {"type": "json", "schema": schema},
    }
    if seed is not None:
        body["seed"] = seed
    started = time.perf_counter()
    response = requests.post(url, headers={
        "Authorization": f"Bearer {api_key}", "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json", "Accept": "application/json",
    }, json=body, timeout=timeout)
    latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        detail = response.text.replace("\n", " ")[:500]
        raise RuntimeError(f"HCX article request failed ({response.status_code}): {detail}")
    payload = response.json()
    content = str(payload.get("result", {}).get("message", {}).get("content", "")).strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("HCX article response does not contain a JSON object")
    return json.loads(content[start:end + 1]), payload.get("result", {}).get("usage", {}) or {}, latency_ms


# The r16i call sites and their tests monkeypatch this name; keeping the alias
# means the split does not touch them.
_call_hcx_json = call_hcx_json


