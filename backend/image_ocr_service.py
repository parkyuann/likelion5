"""이미지 업로드를 검증하고 OCR 전체 본문을 ArticleDocument로 변환한다."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps, UnidentifiedImageError

from backend.errors import BackendError

MAX_UPLOAD_BYTES = int(os.getenv("OCR_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.getenv("OCR_MAX_IMAGE_PIXELS", "40000000"))
MAX_LONG_SIDE = 2240

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _detect_mime(data: bytes) -> str:
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    raise BackendError(
        "UNSUPPORTED_IMAGE_TYPE",
        "PNG, JPEG 또는 WebP 이미지만 업로드할 수 있습니다.",
        status_code=415,
    )


def _decode_image(data: bytes) -> tuple[Image.Image, tuple[int, int], str]:
    media_type = _detect_mime(data)
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise BackendError(
                "IMAGE_RESOLUTION_TOO_LARGE",
                "이미지 해상도가 허용 범위를 초과합니다.",
                status_code=413,
                detail={"max_pixels": MAX_IMAGE_PIXELS},
            )
        image.load()
    except BackendError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise BackendError(
            "INVALID_IMAGE",
            "손상되었거나 읽을 수 없는 이미지입니다.",
            status_code=422,
        ) from exc

    original_size = image.size
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, rgba).convert("RGB")
    elif image.mode != "RGB":
        image = image.convert("RGB")

    long_side = max(image.size)
    if long_side > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / long_side
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    return image, original_size, media_type


def _default_ocr(image: Image.Image) -> dict[str, Any]:
    from backend import clova_ocr_client

    try:
        result = clova_ocr_client.run_ocr(
            image,
            json_mode=False,
            tiles=1,
        )
    except BackendError:
        raise
    result["model"] = clova_ocr_client.MODEL
    result["prompt_version"] = clova_ocr_client.PROMPT_VERSION
    return result


def prepare_image_article(
    data: bytes,
    *,
    filename: str | None,
    declared_content_type: str | None,
    ocr_fn: Callable[[Image.Image], dict[str, Any]] | None = None,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> dict[str, Any]:
    """업로드 이미지를 OCR하고 전체 전사문을 공통 기사 문서 계약으로 반환한다."""
    if not data:
        raise BackendError("EMPTY_IMAGE", "업로드한 이미지가 비어 있습니다.", status_code=422)
    if len(data) > max_upload_bytes:
        raise BackendError(
            "IMAGE_TOO_LARGE",
            "이미지 파일 크기가 허용 범위를 초과합니다.",
            status_code=413,
            detail={"max_bytes": max_upload_bytes},
        )

    image, original_size, detected_content_type = _decode_image(data)
    warnings: list[str] = []
    if declared_content_type and declared_content_type != detected_content_type:
        warnings.append("DECLARED_CONTENT_TYPE_MISMATCH")

    try:
        ocr = (ocr_fn or _default_ocr)(image)
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(
            "OCR_PROCESSING_FAILED",
            "OCR 처리 중 오류가 발생했습니다.",
            status_code=502,
        ) from exc

    raw_text = str(ocr.get("raw_text") or "")
    text = str(ocr.get("normalized_text") or raw_text).strip()
    ocr_status = str(ocr.get("status") or "failed")
    failed_calls = int(ocr.get("failed_calls") or 0)
    if ocr_status == "failed" and failed_calls > 0:
        raise BackendError(
            "OCR_PROCESSING_FAILED",
            "OCR 서비스 호출 중 오류가 발생했습니다.",
            status_code=502,
        )
    if not text or ocr_status == "failed":
        raise BackendError(
            "OCR_TEXT_NOT_FOUND",
            "이미지에서 읽을 수 있는 본문을 찾지 못했습니다.",
            status_code=422,
        )
    if ocr_status == "partial":
        warnings.append("OCR_PARTIAL_RESULT")
    elif ocr_status == "truncated":
        warnings.append("OCR_RESULT_TRUNCATED")

    safe_filename = Path(filename or "upload").name
    content_hash = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    image_sha256 = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return {
        "type": "article_document",
        "status": "ready_for_verification",
        "route": {
            "route": "image_ocr",
            "confidence": 1.0,
            "reason_code": "EXPLICIT_IMAGE_UPLOAD",
        },
        "source": {
            "source_type": "image",
            "filename": safe_filename,
            "content_type": detected_content_type,
            "byte_size": len(data),
            "content_hash": content_hash,
            "image_sha256": image_sha256,
        },
        "extraction": {
            "status": "success" if not warnings else "success_with_warning",
            "ocr_status": ocr_status,
            "raw_text": raw_text,
            "character_count": len(text),
            "warning_codes": warnings,
            "original_width": original_size[0],
            "original_height": original_size[1],
            "processed_width": image.width,
            "processed_height": image.height,
            "calls": int(ocr.get("calls") or 0),
            "failed_calls": failed_calls,
            "latency_ms": float(ocr.get("latency_ms") or 0.0),
            "model": ocr.get("model"),
            "prompt_version": ocr.get("prompt_version"),
        },
        "article_document": {
            "source_type": "image",
            "text": text,
            "title": None,
            "published_date": None,
            "extractor": "hcx_vision_ocr",
            "content_hash": content_hash,
        },
    }
