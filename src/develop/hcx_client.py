"""HCX chat-completions client.

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


def call_hcx_json(*, system_prompt: str, user_prompt: str, schema: dict[str, Any], api_key: str,
                  model: str, timeout: int) -> tuple[dict[str, Any], dict[str, Any], float]:
    if not api_key:
        raise ValueError("HCX API key is required")
    if requests is None:
        raise RuntimeError("requests package is required only for HCX API calls")
    url = f"https://clovastudio.stream.ntruss.com/v3/chat-completions/{model.upper()}"
    body = {
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "temperature": 0.1, "topP": 0.8, "topK": 0, "repetitionPenalty": 1.1,
        # A whole article can contain many independently grounded item claims;
        # 4,000 tokens truncated the JSON response before its closing brace in
        # the 2680 calibration article.  Keep the response schema-constrained
        # but allow the complete audit array to be returned.
        "maxCompletionTokens": 8000, "thinking": {"effort": "none"},
        "responseFormat": {"type": "json", "schema": schema},
    }
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
