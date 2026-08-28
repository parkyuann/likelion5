"""Server-side CLOVA General OCR client.

The browser never receives the OCR Invoke URL or the X-OCR-SECRET. They are
read only from the server runtime environment and are deliberately absent from
Git and API responses.
"""

from __future__ import annotations

import base64
import io
import os
import time
import uuid
from typing import Any

import requests
from PIL import Image

from backend.errors import BackendError


_TIMEOUT_SECONDS = float(os.getenv("CLOVA_OCR_TIMEOUT_SECONDS", "25"))
_MAX_TEXT_CHARS = int(os.getenv("CLOVA_OCR_MAX_TEXT_CHARS", "100000"))


def _configuration() -> tuple[str, str]:
    invoke_url = os.getenv("CLOVA_OCR_INVOKE_URL", "").strip()
    secret = os.getenv("CLOVA_OCR_SECRET", "").strip()
    if not invoke_url or not secret:
        raise BackendError(
            "OCR_NOT_CONFIGURED",
            "이미지 OCR 연결 정보가 아직 설정되지 않았습니다.",
            status_code=503,
        )
    if not invoke_url.startswith("https://"):
        raise BackendError("OCR_CONFIGURATION_INVALID", "OCR 호출 주소 설정이 올바르지 않습니다.", status_code=503)
    return invoke_url, secret


def _image_payload(image: Image.Image) -> str:
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=92, optimize=True)
    return base64.b64encode(encoded.getvalue()).decode("ascii")


def _recognized_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise BackendError("OCR_RESPONSE_INVALID", "OCR 결과 형식이 올바르지 않습니다.", status_code=502)
    images = payload.get("images")
    if not isinstance(images, list) or not images or not isinstance(images[0], dict):
        raise BackendError("OCR_RESPONSE_INVALID", "OCR 결과에 이미지 인식 정보가 없습니다.", status_code=502)
    first = images[0]
    if str(first.get("inferResult") or "").upper() != "SUCCESS":
        raise BackendError("OCR_PROCESSING_FAILED", "OCR 서비스가 이미지를 처리하지 못했습니다.", status_code=502)
    fields = first.get("fields")
    if not isinstance(fields, list):
        raise BackendError("OCR_TEXT_NOT_FOUND", "이미지에서 읽을 수 있는 본문을 찾지 못했습니다.", status_code=422)
    lines: list[str] = []
    current: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        text = str(field.get("inferText") or "").strip()
        if not text:
            continue
        current.append(text)
        if field.get("lineBreak") is True:
            lines.append(" ".join(current))
            current = []
    if current:
        lines.append(" ".join(current))
    value = "\n".join(lines).strip()
    if not value:
        raise BackendError("OCR_TEXT_NOT_FOUND", "이미지에서 읽을 수 있는 본문을 찾지 못했습니다.", status_code=422)
    if len(value) > _MAX_TEXT_CHARS:
        raise BackendError("OCR_TEXT_TOO_LARGE", "OCR로 추출된 본문이 허용 범위를 초과합니다.", status_code=413)
    return value


def run_ocr(image: Image.Image, *, json_mode: bool = False, tiles: int = 1) -> dict[str, Any]:
    """Run exactly one Korean General OCR request and normalize only its text."""

    del json_mode, tiles
    invoke_url, secret = _configuration()
    started = time.monotonic()
    body = {
        "version": "V2",
        "requestId": str(uuid.uuid4()),
        "timestamp": int(time.time() * 1000),
        "lang": "ko",
        "images": [{"format": "jpg", "name": "article", "data": _image_payload(image)}],
    }
    try:
        response = requests.post(
            invoke_url,
            headers={"X-OCR-SECRET": secret, "Content-Type": "application/json"},
            json=body,
            timeout=_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise BackendError("OCR_PROCESSING_FAILED", "OCR 서비스에 연결하지 못했습니다.", status_code=502) from exc
    if response.status_code >= 400:
        raise BackendError("OCR_PROCESSING_FAILED", "OCR 서비스가 요청을 처리하지 못했습니다.", status_code=502)
    try:
        payload = response.json()
    except ValueError as exc:
        raise BackendError("OCR_RESPONSE_INVALID", "OCR 결과 형식이 올바르지 않습니다.", status_code=502) from exc
    text = _recognized_text(payload)
    return {
        "status": "success",
        "raw_text": text,
        "normalized_text": text,
        "calls": 1,
        "failed_calls": 0,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
    }


MODEL = "CLOVA_GENERAL_OCR"
PROMPT_VERSION = "clova-general-ocr-v2"
