"""Configurable HCX claim-extraction experiment runner.

팀 인계: 과거·실험용 HCX 실행기와 API key 조회 보조 모듈이다. 현재 r17 L2 실행은
``src/develop/run_l2_segmentation.py``에서 시작한다.

This is the canonical experiment entry point for the current workflow:
rule-based candidate CSV -> HCX structured extraction -> Codex silver comparison.
Each run is isolated by experiment_id and records latency, token usage, estimated
cost, raw prediction, and evaluation metrics.  No API call is made at import.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieval_schema import (  # noqa: E402  통제 어휘·게이트 정본
    CLAIM_CLASSES,
    NOISE_REASONS,
    compute_verifiability_prefilter,
)
from source_scope_classifier import (  # noqa: E402  source_scope는 LLM이 아니라 사전으로 결정
    classify_source_scope,
    load_kosis_org_catalog,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "evaluation" / "pilot120.csv"
DEFAULT_OUTPUT_DIR = ROOT / "output" / "hcx_experiments"
DEFAULT_METRICS_LOG = ROOT / "data" / "baseline" / "hcx_experiments" / "hcx_experiment_metrics.jsonl"
DEFAULT_SUMMARY_FILE = ROOT / "data" / "baseline" / "hcx_experiments" / "hcx_experiment_summary.csv"
MODEL_DEFAULT = "HCX-003"
RLT_LATENCY_REF_MS_DEFAULT = 6112.722
RLT_TOKENS_REF_DEFAULT = 412.746
MODEL_ALIASES = {
    "HCX-BASH-001": "HCX-DASH-001",
    "HCX-BASH-002": "HCX-DASH-002",
}
V1_MODELS = {"HCX-003", "HCX-DASH-001"}
RESPONSE_FORMAT_MODELS = {"HCX-007"}

# relation_type 통제 어휘 — retrieval_schema.RELATION_TYPES와 동일해야 한다.
# 프롬프트 유도만으로는 모델이 어휘 밖 값(예: current_value)을 지어내므로
# (a) responseFormat enum, (b) 파싱 후 clamp 두 겹으로 방어한다.
RELATION_TYPES = ("primary", "comparison_base", "component", "total", "rank_peer", "untyped")

# claim_class 통제 어휘 — 정본은 retrieval_schema.CLAIM_CLASSES. 아래는 프롬프트 제시용
# 순서·정의이며, 집합이 정본과 어긋나면 로드 시점에 즉시 실패한다(단일 출처 보증).
# 노이즈·비주장은 별도 claim_class가 아니라 is_claim=false + claim_class="" 로 처리한다.
CLAIM_CLASS_DEFINITIONS = (
    ("집계통계", "인구·고용·물가·무역·복지·교육 등 집단/기간을 집계한 관측 통계(공식 통계표로 확인 가능). KOSIS 대조 대상."),
    ("개별사례", "개별 기업·개인·사건 1건의 수치(매출·투자·주가·지분 등)."),
    ("전망예측", "미래 전망·예측치."),
    ("목표계획", "정책·기업의 목표·계획 수치(아직 실현되지 않음)."),
    ("법령제도", "법령·제도가 정한 기준값(세율·최저임금·벌금 등)."),
    ("해석수사", "정확한 통계값 없이 수사적으로 비교·강조하는 서술."),
    ("사고대응임시통계", "재난·사고 대응 중 발표되는 임시 집계(예: 화재 복구율)."),
    ("여론조사", "지지율·경선 반영비율 등 여론조사 수치."),
    ("통계조사안내", "통계조사의 실시·발표 일정 등 조사 자체 안내(결과값 아님)."),
    ("정정보도", "앞선 보도의 오류를 바로잡는 정정 공지(검증 대상 아님)."),
)
CLAIM_CLASS_ORDER = [name for name, _ in CLAIM_CLASS_DEFINITIONS]
assert set(CLAIM_CLASS_ORDER) == set(CLAIM_CLASSES), (
    f"claim_class 어휘가 retrieval_schema.CLAIM_CLASSES와 불일치: "
    f"{set(CLAIM_CLASS_ORDER) ^ set(CLAIM_CLASSES)}"
)
_CLAIM_CLASS_BLOCK = "\n".join(f"   - {name}: {desc}" for name, desc in CLAIM_CLASS_DEFINITIONS)

# noise_reason 통제 어휘 — 정본은 retrieval_schema.NOISE_REASONS. is_claim=False 진단축.
NOISE_REASON_DEFINITIONS = (
    ("광고", "광고·홍보·판촉 문구"),
    ("의견", "검증 가능한 수치가 아닌 개인 의견·논평"),
    ("질문", "의문문·질의"),
    ("불완전문", "추출 오류로 잘린 문장 조각·비문"),
    ("인용맥락", "다른 화자 발언의 인용 맥락이라 수치 주장으로 귀속 불가"),
    ("UI잡음", "재생목록·메뉴·캡션 등 UI/비기사 텍스트"),
    ("기타", "위에 해당하지 않는 비주장"),
)
NOISE_REASON_ORDER = [name for name, _ in NOISE_REASON_DEFINITIONS]
assert set(NOISE_REASON_ORDER) == set(NOISE_REASONS), (
    f"noise_reason 어휘가 retrieval_schema.NOISE_REASONS와 불일치: "
    f"{set(NOISE_REASON_ORDER) ^ set(NOISE_REASONS)}"
)
_NOISE_REASON_BLOCK = "\n".join(f"     - {name}: {desc}" for name, desc in NOISE_REASON_DEFINITIONS)

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "is_claim": {"type": "boolean"},
        "claim_class": {"type": ["string", "null"], "enum": CLAIM_CLASS_ORDER + [None]},
        "noise_reason": {"type": ["string", "null"], "enum": NOISE_REASON_ORDER + [None]},
        "indicator_raw": {"type": ["string", "null"]},
        "population": {"type": ["string", "null"]},
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": ["string", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "period": {"type": ["string", "null"]},
                    "time_compare": {"type": ["string", "null"]},
                    "dimension": {"type": "object"},
                    "relation_type": {"type": "string", "enum": list(RELATION_TYPES)},
                    "comparison_group": {"type": ["string", "null"]},
                    "sequence": {"type": "integer"},
                },
                "required": ["value", "unit", "period", "relation_type", "comparison_group", "sequence"],
            },
        },
        "source_org_raw": {"type": ["string", "null"]},
        "source_role": {"type": ["string", "null"]},
        "evidence_quote": {"type": "string"},
    },
    "required": [
        "is_claim",
        "claim_class",
        "indicator_raw",
        "population",
        "observations",
        "source_org_raw",
        "source_role",
        "evidence_quote",
    ],
}

SYSTEM_PROMPT = f"""
당신은 규칙 기반으로 추출된 후보 문장을 구조화하는 통계 claim 분석기입니다.
입력 문장만 근거로 판단하고, 근거가 없으면 null 또는 빈 배열을 사용합니다.

1. 먼저 검증 가능한 수치 주장인지 is_claim(true/false)으로 판정합니다. 판정 결과에 따라 두 필드를 조건부로 채웁니다.
   - is_claim=false이면(광고·의견·질문·불완전문·인용맥락·UI잡음 등 검증 대상이 아닌 경우) claim_class는 null로 두고, noise_reason을 아래 목록에서 정확히 하나 고릅니다:
{_NOISE_REASON_BLOCK}
   - is_claim=true이면 noise_reason은 null로 두고, claim_class를 아래 10종에서 정확히 하나만 고릅니다(목록 밖의 값을 새로 만들지 않습니다). 애매하면 노이즈로 버리지 말고 가장 가까운 유형을 고릅니다:
{_CLAIM_CLASS_BLOCK}
   집계통계만 KOSIS 대조 대상입니다.
2. is_claim=true이면 indicator_raw, population을 추출합니다.
3. 한 문장에 수치가 여러 개 있으면 claims를 여러 행으로 나누지 말고 observations 배열에 모두 기록합니다.
4. 각 observation은 value, unit, period, time_compare, dimension, relation_type, comparison_group, sequence를 가집니다.
5. relation_type은 관측값이 주장 안에서 갖는 역할이며 primary, comparison_base,
   component, total, rank_peer, untyped 중 하나입니다. 비교(예: 올해 vs 작년)는
   기준값(primary)과 비교대상값(comparison_base)을 같은 comparison_group으로 묶고
   sequence로 순서를 매겨 표현합니다. 구성/합계는 component/total, 순위 목록의
   동료 항목은 rank_peer, 역할이 불분명하면 untyped를 씁니다.
6. source_org_raw와 source_role은 문장에 근거가 있을 때만 기록합니다. source_scope는 출력하지 않습니다(시스템이 기관 사전으로 결정론적으로 판정).
7. evidence_quote는 입력 문장에서 직접 인용한 짧은 근거입니다.
8. JSON 객체 하나만 출력합니다.
""".strip()


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def load_project_env() -> None:
    """Load API keys from the project-root .env without printing secrets."""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and value:
            os.environ.setdefault(key, value)


def env_api_key() -> str:
    load_project_env()
    return (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()


def normalize_model_name(model: str) -> str:
    name = model.strip().upper()
    return MODEL_ALIASES.get(name, name)


def infer_api_version(model: str) -> str:
    return "v1" if model in V1_MODELS else "v3"


def effective_temperature(temperature: float, version: str) -> float:
    # v1 rejects an exact zero temperature; v3 accepts it.
    return 0.1 if version == "v1" and temperature <= 0 else temperature


def experiment_id(
    model: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    prompt_version: str,
    input_path: Path | None = None,
    row_count: int | None = None,
    response_format: bool = False,
    rlt_signature: tuple[float, float, float, float, float] | None = None,
    label_signature: str | None = None,
) -> str:
    dataset = input_path.stem if input_path else "dataset"
    safe_dataset = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in dataset)
    safe_prompt = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in prompt_version)
    raw = json.dumps({
        "model": model,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "prompt_version": prompt_version,
        "input": str(input_path) if input_path else "",
        "row_count": row_count,
        "response_format": response_format,
        "rlt_signature": rlt_signature,
        "label_signature": label_signature,
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    safe_model = model.replace("/", "-").replace(" ", "-")
    count_tag = f"n{row_count}" if row_count is not None else "nall"
    return f"{safe_dataset}_{safe_model}_t{temperature:g}_p{top_p:g}_{safe_prompt}_{count_tag}_h{digest}"


def parse_content(payload: dict) -> tuple[dict, dict]:
    result = payload.get("result", payload)
    content = result.get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise ValueError("HCX response content is empty")
    text = str(content).strip().lstrip("\ufeff")
    if text.startswith("```"):
        text = text.replace("```json", "", 1).replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("HCX response does not contain a JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("HCX response JSON is not an object")
    return parsed, result.get("usage", {}) or payload.get("usage", {}) or {}


def clamp_relation_types(prediction: dict) -> int:
    """어휘 밖 relation_type을 untyped로 강등하고 강등 건수를 반환한다.

    cache.jsonl에는 HCX 원본이 그대로 남고, 이 함수는 소비 지점에서 prediction을
    제자리 수정해 predictions.csv·metrics만 정제한다. 원본 라벨은
    relation_type_raw로 보존해 감사 가능하게 한다.
    """
    if not isinstance(prediction, dict):
        return 0
    clamped = 0
    for observation in prediction.get("observations", []) or []:
        if not isinstance(observation, dict):
            continue
        value = observation.get("relation_type")
        if value not in RELATION_TYPES:
            observation["relation_type_raw"] = value
            observation["relation_type"] = "untyped"
            clamped += 1
    return clamped


def normalize_label(prediction: dict) -> dict[str, int]:
    """is_claim/claim_class/noise_reason 조건부 계층을 제자리에서 정합화한다.

    규칙(retrieval_schema.validate_claim와 동일):
      is_claim=True  ⇒ claim_class는 10종 중 하나(어휘 밖은 null로 강등), noise_reason=null
      is_claim=False ⇒ claim_class=null, noise_reason은 통제 어휘(어휘 밖은 "기타")

    프롬프트 조건부 지시가 1차 방어, 이 함수는 안전망이다. 강등된 원본은 *_raw로
    보존해 감사 가능하게 하고, 강등 사유별 건수를 반환한다.
    """
    counts = {"claim_class_oov": 0, "noise_reason_oov": 0, "consistency_fix": 0}
    if not isinstance(prediction, dict):
        return counts
    is_claim = bool(prediction.get("is_claim"))
    claim_class = prediction.get("claim_class") or None
    noise_reason = prediction.get("noise_reason") or None

    if is_claim:
        if noise_reason is not None:  # 주장인데 noise_reason이 있으면 모순
            prediction["noise_reason_raw"] = noise_reason
            noise_reason = None
            counts["consistency_fix"] += 1
        if claim_class is not None and claim_class not in CLAIM_CLASSES:
            prediction["claim_class_raw"] = claim_class
            claim_class = None
            counts["claim_class_oov"] += 1
    else:
        if claim_class is not None:  # 비주장인데 claim_class가 있으면 모순
            prediction["claim_class_raw"] = claim_class
            claim_class = None
            counts["consistency_fix"] += 1
        if noise_reason is not None and noise_reason not in NOISE_REASONS:
            prediction["noise_reason_raw"] = noise_reason
            noise_reason = "기타"
            counts["noise_reason_oov"] += 1

    prediction["claim_class"] = claim_class
    prediction["noise_reason"] = noise_reason
    return counts


def tokenize_messages(
    api_key: str,
    model: str,
    api_version: str,
    messages: list[dict],
    timeout: int = 30,
) -> tuple[int, float]:
    """Count message tokens through the official CLOVA tokenizer API."""
    url = f"https://clovastudio.stream.ntruss.com/{api_version}/api-tools/chat-tokenize/{model}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    started = time.perf_counter()
    response = requests.post(url, headers=headers, json={"messages": messages}, timeout=timeout)
    latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:500]
        raise RuntimeError(f"HCX tokenizer failed ({response.status_code}) body={detail}")
    payload = response.json()
    counted = payload.get("result", {}).get("messages", [])
    total = sum(int(item.get("count", 0)) for item in counted if isinstance(item, dict))
    return total, latency_ms


def call_hcx(
    claim_text: str,
    api_key: str,
    model: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    prompt: str,
    api_version: str = "auto",
    use_response_format: bool = False,
    timeout: int = 120,
) -> tuple[dict, dict, float]:
    model = normalize_model_name(model)
    version = infer_api_version(model) if api_version == "auto" else api_version
    if version not in {"v1", "v3"}:
        raise ValueError("api_version must be auto, v1, or v3")
    url = f"https://clovastudio.stream.ntruss.com/{version}/chat-completions/{model}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"후보 claim 문장:\n{claim_text}"},
        ],
        "temperature": effective_temperature(temperature, version),
        "topP": top_p,
        "topK": 0,
    }
    if version == "v1":
        body.update({"maxTokens": max_tokens, "repetitionPenalty": 1.1, "stop": []})
    else:
        body.update({"repetitionPenalty": 1.1, "maxCompletionTokens": max_tokens})
        # DASH-002 and HCX-005 reject responseFormat; HCX-007 supports it.
        if model in RESPONSE_FORMAT_MODELS:
            # Disable model-specific reasoning so the comparison measures the
            # same direct extraction task as the non-reasoning HCX models.
            body["thinking"] = {"effort": "none"}
            if use_response_format:
                body["responseFormat"] = {"type": "json", "schema": CLAIM_SCHEMA}
    started = time.perf_counter()
    response = requests.post(url, headers=headers, json=body, timeout=timeout)
    latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:1000]
        raise RuntimeError(
            f"HCX request failed ({response.status_code}) url={url} body={detail}"
        )
    parsed, usage = parse_content(response.json())
    return parsed, usage, latency_ms


def usage_value(usage: dict, *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def normalize_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def predicted_is_claim(prediction: dict) -> bool:
    if "is_claim" in prediction:
        return bool(prediction["is_claim"])
    return clean(prediction.get("claim_class")) not in {"", "노이즈", "통계조사안내"}


def gold_is_claim(row: pd.Series) -> bool:
    explicit = clean(row.get("gold_is_claim"))
    if explicit:  # 신 스키마 평가셋: is_claim 축을 명시 컬럼에서 직접 읽는다.
        return normalize_bool(explicit)
    claim_class = clean(row.get("gold_claim_class"))
    return claim_class not in {"", "노이즈", "통계조사안내"}


def f1_for_binary(y_true: list[bool], y_pred: list[bool]) -> dict[str, float]:
    tp = sum(a and b for a, b in zip(y_true, y_pred))
    fp = sum((not a) and b for a, b in zip(y_true, y_pred))
    fn = sum(a and (not b) for a, b in zip(y_true, y_pred))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 6), "recall": round(recall, 6), "f1": round(f1, 6)}


def macro_f1(y_true: list[str], y_pred: list[str]) -> float:
    labels = sorted(set(y_true) | set(y_pred))
    if not labels:
        return 0.0
    scores = []
    for label in labels:
        score = f1_for_binary([x == label for x in y_true], [x == label for x in y_pred])
        scores.append(score["f1"])
    return round(sum(scores) / len(scores), 6)


def macro_prf(y_true: list[str], y_pred: list[str]) -> dict[str, float]:
    labels = sorted(set(y_true) | set(y_pred))
    if not labels:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    scores = [
        f1_for_binary([x == label for x in y_true], [x == label for x in y_pred])
        for label in labels
    ]
    return {key: round(sum(item[key] for item in scores) / len(scores), 6)
            for key in ("precision", "recall", "f1")}


def calculate_metrics(rows: list[dict]) -> dict:
    usable = [row for row in rows if row.get("status") == "ok"]
    gold_bool = [row["gold_is_claim"] for row in usable]
    pred_bool = [row["pred_is_claim"] for row in usable]
    gold_class = [row["gold_claim_class"] for row in usable]
    pred_class = [row["pred_claim_class"] for row in usable]
    class_accuracy = sum(a == b for a, b in zip(gold_class, pred_class)) / len(gold_class) if gold_class else 0.0
    class_prf = macro_prf(gold_class, pred_class)
    scope_prf = macro_prf(
        [row["gold_source_scope"] for row in usable],
        [row["pred_source_scope"] for row in usable],
    )
    return {
        "rows_total": len(rows),
        "rows_ok": len(usable),
        "rows_error": len(rows) - len(usable),
        "claim_detection": f1_for_binary(gold_bool, pred_bool),
        "claim_class_accuracy": round(class_accuracy, 6),
        "claim_class_macro_precision": class_prf["precision"],
        "claim_class_macro_recall": class_prf["recall"],
        "claim_class_macro_f1": class_prf["f1"],
        "source_scope_macro_precision": scope_prf["precision"],
        "source_scope_macro_recall": scope_prf["recall"],
        "source_scope_macro_f1": scope_prf["f1"],
        "source_scope_exact": round(
            sum(row["gold_source_scope"] == row["pred_source_scope"] for row in usable) / len(usable), 6
        ) if usable else 0.0,
        "source_role_exact": round(
            sum(row["gold_source_role"] == row["pred_source_role"] for row in usable) / len(usable), 6
        ) if usable else 0.0,
        "unit_exact_available": round(
            sum(row["gold_unit"] == row["pred_unit"] for row in usable if row["gold_unit"] or row["pred_unit"])
            / max(1, sum(bool(row["gold_unit"] or row["pred_unit"]) for row in usable)),
            6,
        ),
        "total_latency_ms": round(sum(row.get("latency_ms", 0.0) for row in rows), 3),
        "mean_latency_ms_ok": round(sum(row.get("latency_ms", 0.0) for row in usable) / len(usable), 3) if usable else 0.0,
        "total_inference_latency_ms": round(sum(row.get("inference_latency_ms", row.get("latency_ms", 0.0)) for row in rows), 3),
        "total_tokenizer_latency_ms": round(sum(row.get("tokenizer_latency_ms", 0.0) for row in rows), 3),
        "mean_inference_latency_ms_ok": round(sum(row.get("inference_latency_ms", row.get("latency_ms", 0.0)) for row in usable) / len(usable), 3) if usable else 0.0,
        "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in rows),
        "completion_tokens": sum(row.get("completion_tokens", 0) for row in rows),
        "total_tokens": sum(row.get("total_tokens", 0) for row in rows),
        "mean_total_tokens_ok": round(sum(row.get("total_tokens", 0) for row in usable) / len(usable), 3) if usable else 0.0,
        "estimated_cost": round(sum(row.get("estimated_cost", 0.0) for row in rows), 8),
    }


def calculate_rlt_score(
    metrics: dict,
    latency_ref_ms: float,
    tokens_ref: float,
    recall_weight: float,
    latency_weight: float,
    tokens_weight: float,
) -> float | None:
    """Calculate the supplementary Recall-Latency-Token trade-off score."""
    recall = metrics["claim_detection"]["recall"]
    # RLT recall is the mean recall across claim detection, claim class, and
    # source scope so model selection reflects the requested target fields.
    recall = sum([
        metrics["claim_detection"]["recall"],
        metrics["claim_class_macro_recall"],
        metrics["source_scope_macro_recall"],
    ]) / 3
    latency = metrics["mean_inference_latency_ms_ok"]
    tokens = metrics["mean_total_tokens_ok"]
    if latency <= 0 or tokens <= 0 or latency_ref_ms <= 0 or tokens_ref <= 0:
        return None
    latency_efficiency = 1 / (1 + latency / latency_ref_ms)
    token_efficiency = 1 / (1 + tokens / tokens_ref)
    score = (
        recall ** recall_weight
        * latency_efficiency ** latency_weight
        * token_efficiency ** tokens_weight
    )
    return round(score, 6)


def update_experiment_summary(metrics: dict, summary_path: Path) -> None:
    """Upsert one flattened metrics row per experiment for easy comparison."""
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    row = {}
    for key, value in metrics.items():
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                row[f"{key}_{child_key}"] = child_value
        else:
            row[key] = value
    current = pd.read_csv(summary_path, keep_default_na=False) if summary_path.exists() else pd.DataFrame()
    if not current.empty and "experiment_id" in current.columns:
        current = current[current["experiment_id"] != row["experiment_id"]]
    combined = pd.concat([current, pd.DataFrame([row])], ignore_index=True, sort=False)
    combined.to_csv(summary_path, index=False, encoding="utf-8-sig")


def write_progress(
    path: Path,
    *,
    experiment_id: str,
    model: str,
    dataset: str,
    completed: int,
    total: int,
    started_perf: float,
    ok: int,
    errors: int,
    current_claim_id: str = "",
    status: str = "running",
) -> None:
    """Persist resumable batch progress and an ETA for external monitoring."""
    elapsed = max(0.0, time.perf_counter() - started_perf)
    rate = completed / elapsed if completed and elapsed > 0 else 0.0
    remaining = max(0, total - completed)
    eta_seconds = remaining / rate if rate > 0 else None
    eta = None
    if eta_seconds is not None:
        eta = datetime.now(timezone.utc).timestamp() + eta_seconds
        eta = datetime.fromtimestamp(eta, timezone.utc).isoformat()
    payload = {
        "experiment_id": experiment_id,
        "model": model,
        "dataset": dataset,
        "status": status,
        "completed": completed,
        "total": total,
        "remaining": remaining,
        "progress_percent": round(completed / total * 100, 2) if total else 100.0,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_rows_per_second": round(rate, 6),
        "estimated_remaining_seconds": round(eta_seconds, 3) if eta_seconds is not None else None,
        "eta_utc": eta,
        "ok": ok,
        "errors": errors,
        "current_claim_id": current_claim_id,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if status == "running":
        eta_text = eta or "계산 중"
        eta_minutes = f"{eta_seconds / 60:.1f}분" if eta_seconds is not None else "계산 중"
        print(
            f"[진행률] {model} {dataset} {completed}/{total} "
            f"({payload['progress_percent']:.2f}%) | "
            f"경과 {elapsed / 60:.1f}분 | 잔여 약 "
            f"{eta_minutes} | ETA {eta_text}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY_FILE)
    parser.add_argument("--metrics-log", type=Path, default=DEFAULT_METRICS_LOG)
    parser.add_argument("--model", default=MODEL_DEFAULT)
    parser.add_argument("--experiment-variant", default="", help="Configuration variant identifier for comparison and final selection")
    parser.add_argument("--api-version", choices=["auto", "v1", "v3"], default="auto")
    parser.add_argument(
        "--use-response-format",
        action="store_true",
        help="지원 모델에만 JSON Schema responseFormat을 적용합니다. 모델 공정 비교에서는 사용하지 않습니다.",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--max-tokens", type=int, default=1800)
    parser.add_argument("--prompt-version", default="claim-observation-v1")
    parser.add_argument("--system-prompt-file", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--input-cost-per-1m", type=float, default=0.0)
    parser.add_argument("--output-cost-per-1m", type=float, default=0.0)
    parser.add_argument("--skip-tokenizer", action="store_true", help="Skip tokenizer API fallback when usage is absent")
    parser.add_argument("--latency-ref-ms", type=float, default=RLT_LATENCY_REF_MS_DEFAULT)
    parser.add_argument("--tokens-ref", type=float, default=RLT_TOKENS_REF_DEFAULT)
    parser.add_argument("--rlt-recall-weight", type=float, default=0.6)
    parser.add_argument("--rlt-latency-weight", type=float, default=0.2)
    parser.add_argument("--rlt-tokens-weight", type=float, default=0.2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    prompt = args.system_prompt_file.read_text(encoding="utf-8") if args.system_prompt_file else SYSTEM_PROMPT
    args.model = normalize_model_name(args.model)
    input_df = pd.read_csv(args.input, keep_default_na=False)
    df = input_df.iloc[args.offset : args.offset + args.limit if args.limit else None].copy()
    gold_columns = [column for column in df.columns if column.startswith("gold_")]
    label_payload = df[gold_columns].astype(str).to_csv(index=False) if gold_columns else "no-gold-columns"
    label_signature = hashlib.sha1(label_payload.encode("utf-8")).hexdigest()[:12]
    rlt_signature = (
        args.latency_ref_ms,
        args.tokens_ref,
        args.rlt_recall_weight,
        args.rlt_latency_weight,
        args.rlt_tokens_weight,
    )
    resolved_api_version = infer_api_version(args.model) if args.api_version == "auto" else args.api_version
    exp_id = experiment_id(
        args.model,
        args.temperature,
        args.top_p,
        args.max_tokens,
        args.prompt_version,
        input_path=args.input,
        row_count=len(df),
        response_format=args.use_response_format,
        rlt_signature=rlt_signature,
        label_signature=label_signature,
    )
    output_dir = args.output_dir / exp_id
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "cache.jsonl"
    result_path = output_dir / "predictions.csv"
    metrics_path = output_dir / "metrics.json"

    config = {
        "experiment_id": exp_id,
        "input": str(args.input),
        "input_rows_total": len(input_df),
        "input_rows_selected": len(df),
        "offset": args.offset,
        "limit": args.limit,
        "model": args.model,
        "experiment_variant": args.experiment_variant,
        "api_version": args.api_version,
        "resolved_api_version": resolved_api_version,
        "use_response_format": args.use_response_format,
        "temperature": args.temperature,
        "effective_temperature": effective_temperature(args.temperature, resolved_api_version),
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "prompt_version": args.prompt_version,
        "system_prompt_file": str(args.system_prompt_file) if args.system_prompt_file else None,
        "system_prompt_sha1": hashlib.sha1(prompt.encode("utf-8")).hexdigest(),
        "label_signature": label_signature,
        "seed": args.seed,
        "skip_tokenizer": args.skip_tokenizer,
        "input_cost_per_1m": args.input_cost_per_1m,
        "output_cost_per_1m": args.output_cost_per_1m,
        "rlt": {
            "latency_ref_ms": args.latency_ref_ms,
            "tokens_ref": args.tokens_ref,
            "recall_weight": args.rlt_recall_weight,
            "latency_weight": args.rlt_latency_weight,
            "tokens_weight": args.rlt_tokens_weight,
        },
    }
    (output_dir / "run_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "system_prompt.txt").write_text(prompt, encoding="utf-8")
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                cache[item["eval_claim_id"]] = item

    api_key = env_api_key()
    org_catalog = load_kosis_org_catalog(ROOT / "data" / "kosis_org_names.json")
    rows: list[dict] = []
    batch_started_perf = time.perf_counter()
    progress_path = output_dir / "progress.json"
    write_progress(
        progress_path, experiment_id=exp_id, model=args.model,
        dataset=args.input.stem, completed=0, total=len(df),
        started_perf=batch_started_perf, ok=0, errors=0, status="running",
    )
    oov_total = 0
    class_oov_total = 0
    noise_oov_total = 0
    consistency_fix_total = 0
    prefilter_counts: dict[str, int] = {"검증시도": 0, "강등": 0, "제외": 0}
    with cache_path.open("a", encoding="utf-8") as cache_file:
        for _, source in df.iterrows():
            claim_id = clean(source.get("eval_claim_id"))
            # Reuse successful results only. Failed rows must be retried after
            # fixing credentials, request parameters, or transient API errors.
            if claim_id in cache and cache[claim_id].get("status") == "ok":
                result = cache[claim_id]
            else:
                started = time.perf_counter()
                try:
                    if args.dry_run:
                        prediction, usage, latency_ms = ({"is_claim": False, "claim_class": "DRY_RUN", "observations": []}, {}, 0.0)
                    else:
                        if not api_key:
                            raise RuntimeError("HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY가 필요합니다.")
                        prediction, usage, latency_ms = call_hcx(
                            clean(source.get("claim_text")), api_key, args.model,
                            args.temperature, args.top_p, args.max_tokens, prompt, args.api_version,
                            args.use_response_format,
                        )
                    inference_latency_ms = latency_ms
                    prompt_tokens = usage_value(usage, "promptTokens", "prompt_tokens", "inputTokens", "input_tokens")
                    completion_tokens = usage_value(usage, "completionTokens", "completion_tokens", "outputTokens", "output_tokens")
                    total_tokens = usage_value(usage, "totalTokens", "total_tokens") or prompt_tokens + completion_tokens
                    tokenizer_latency_ms = 0.0
                    token_source = "api_usage" if total_tokens > 0 else "unavailable"
                    if not args.dry_run and not args.skip_tokenizer and total_tokens <= 0:
                        try:
                            version = infer_api_version(args.model) if args.api_version == "auto" else args.api_version
                            prompt_messages = [
                                {"role": "system", "content": prompt},
                                {"role": "user", "content": f"후보 claim 문장:\n{clean(source.get('claim_text'))}"},
                            ]
                            assistant_message = {"role": "assistant", "content": json.dumps(prediction, ensure_ascii=False)}
                            prompt_count, prompt_tokenizer_latency = tokenize_messages(api_key, args.model, version, prompt_messages)
                            total_count, completion_tokenizer_latency = tokenize_messages(api_key, args.model, version, prompt_messages + [assistant_message])
                            prompt_tokens = prompt_count
                            total_tokens = total_count
                            completion_tokens = max(0, total_count - prompt_count)
                            tokenizer_latency_ms = prompt_tokenizer_latency + completion_tokenizer_latency
                            token_source = "tokenizer_api"
                        except Exception:
                            # Token measurement must not turn a successful
                            # inference into a failed prediction.
                            token_source = "unavailable"
                    estimated_cost = prompt_tokens / 1_000_000 * args.input_cost_per_1m + completion_tokens / 1_000_000 * args.output_cost_per_1m
                    result = {
                        "eval_claim_id": claim_id,
                        "status": "ok",
                        "prediction": prediction,
                        "latency_ms": round(inference_latency_ms or (time.perf_counter() - started) * 1000, 3),
                        "inference_latency_ms": round(inference_latency_ms, 3),
                        "tokenizer_latency_ms": round(tokenizer_latency_ms, 3),
                        "token_source": token_source,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "estimated_cost": estimated_cost,
                    }
                except Exception as error:  # keep batch progress and record failures
                    result = {"eval_claim_id": claim_id, "status": "error", "error": str(error), "latency_ms": (time.perf_counter() - started) * 1000, "inference_latency_ms": (time.perf_counter() - started) * 1000, "tokenizer_latency_ms": 0.0, "token_source": "unavailable", "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated_cost": 0.0, "prediction": {}}
                cache_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                cache_file.flush()

            prediction = result.get("prediction", {})
            relation_type_oov = clamp_relation_types(prediction)
            label_counts = normalize_label(prediction)
            pred_prefilter = ""
            if isinstance(prediction, dict):
                # source_scope는 LLM 산출을 무시하고 기관 사전으로 결정론 판정한다.
                prediction["source_scope"] = classify_source_scope(
                    prediction.get("source_org_raw"), org_catalog
                ).scope
                # 검색 앞단 디부스트 게이트(3값)를 결정론으로 계산한다.
                pred_prefilter, prefilter_reason = compute_verifiability_prefilter(
                    prediction.get("is_claim"),
                    prediction.get("claim_class"),
                    prediction.get("source_scope"),
                )
                prediction["verifiability_prefilter"] = pred_prefilter
                prediction["verifiability_prefilter_reason"] = prefilter_reason
                prefilter_counts[pred_prefilter] += 1
            class_oov = label_counts["claim_class_oov"]
            oov_total += relation_type_oov
            class_oov_total += class_oov
            noise_oov_total += label_counts["noise_reason_oov"]
            consistency_fix_total += label_counts["consistency_fix"]
            observations = prediction.get("observations", []) if isinstance(prediction, dict) else []
            pred_unit = ";".join(clean(item.get("unit")) for item in observations if clean(item.get("unit")))
            row_result = {
                **result,
                "claim_text": clean(source.get("claim_text")),
                "gold_is_claim": gold_is_claim(source),
                "pred_is_claim": predicted_is_claim(prediction),
                "gold_claim_class": clean(source.get("gold_claim_class")),
                "pred_claim_class": clean(prediction.get("claim_class")),
                "gold_source_scope": clean(source.get("gold_source_scope")),
                "pred_source_scope": clean(prediction.get("source_scope")),
                "gold_source_role": clean(source.get("gold_source_role")),
                "pred_source_role": clean(prediction.get("source_role")),
                "gold_unit": clean(source.get("gold_unit")),
                "pred_unit": pred_unit,
                "pred_noise_reason": clean(prediction.get("noise_reason")),
                "pred_verifiability_prefilter": pred_prefilter,
                "relation_type_oov": relation_type_oov,
                "claim_class_oov": class_oov,
                "noise_reason_oov": label_counts["noise_reason_oov"],
                "consistency_fix": label_counts["consistency_fix"],
                "prediction_json": json.dumps(prediction, ensure_ascii=False),
            }
            row_result.pop("prediction", None)
            rows.append(row_result)
            completed = len(rows)
            ok_count = sum(item.get("status") == "ok" for item in rows)
            error_count = completed - ok_count
            write_progress(
                progress_path, experiment_id=exp_id, model=args.model,
                dataset=args.input.stem, completed=completed, total=len(df),
                started_perf=batch_started_perf, ok=ok_count, errors=error_count,
                current_claim_id=claim_id,
            )

    output_df = pd.DataFrame(rows)
    output_df.to_csv(result_path, index=False, encoding="utf-8-sig")
    experiment_metadata = {
        "experiment_id": exp_id,
        "model": args.model,
        "experiment_variant": args.experiment_variant,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "prompt_version": args.prompt_version,
        "use_response_format": args.use_response_format,
        "input_file": str(args.input),
        "seed": args.seed,
    }
    metrics = calculate_metrics(rows)
    metrics["wall_clock_seconds"] = round(time.perf_counter() - batch_started_perf, 3)
    metrics["relation_type_oov_total"] = oov_total
    metrics["claim_class_oov_total"] = class_oov_total
    metrics["noise_reason_oov_total"] = noise_oov_total
    metrics["consistency_fix_total"] = consistency_fix_total
    metrics["verifiability_prefilter_dist"] = prefilter_counts
    resolved_version = resolved_api_version
    rlt_weight_total = args.rlt_recall_weight + args.rlt_latency_weight + args.rlt_tokens_weight
    if abs(rlt_weight_total - 1.0) > 1e-9:
        raise ValueError("RLT weights must sum to 1.0")
    metrics.update({**experiment_metadata, "api_version": args.api_version, "resolved_api_version": resolved_version, "effective_temperature": effective_temperature(args.temperature, resolved_version), "latency_ref_ms": args.latency_ref_ms, "tokens_ref": args.tokens_ref, "rlt_recall_weight": args.rlt_recall_weight, "rlt_latency_weight": args.rlt_latency_weight, "rlt_tokens_weight": args.rlt_tokens_weight, "rlt_score": calculate_rlt_score(metrics, args.latency_ref_ms, args.tokens_ref, args.rlt_recall_weight, args.rlt_latency_weight, args.rlt_tokens_weight), "input": str(args.input), "input_cost_per_1m": args.input_cost_per_1m, "output_cost_per_1m": args.output_cost_per_1m})
    metrics["rlt_recall"] = round((
        metrics["claim_detection"]["recall"]
        + metrics["claim_class_macro_recall"]
        + metrics["source_scope_macro_recall"]
    ) / 3, 6)
    update_experiment_summary(metrics, args.summary_file)
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    args.metrics_log.parent.mkdir(parents=True, exist_ok=True)
    with args.metrics_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(metrics, ensure_ascii=False) + "\n")
    write_progress(
        progress_path, experiment_id=exp_id, model=args.model,
        dataset=args.input.stem, completed=len(rows), total=len(df),
        started_perf=batch_started_perf,
        ok=sum(item.get("status") == "ok" for item in rows),
        errors=sum(item.get("status") != "ok" for item in rows),
        status="completed",
    )
    print(json.dumps({"experiment_id": exp_id, "predictions": str(result_path), "summary_file": str(args.summary_file), "metrics": str(metrics_path), **metrics}, ensure_ascii=False))


if __name__ == "__main__":
    main()
