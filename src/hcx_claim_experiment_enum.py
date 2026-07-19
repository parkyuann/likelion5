"""Configurable HCX claim-extraction experiment runner (enum-constrained schema).

This is a self-contained variant of hcx_claim_experiment.py: it does NOT import
the original module and can fully replace it in the workflow. The only intentional
differences from the original are:

  1. CLAIM_SCHEMA constrains claim_class / source_scope / source_role / relation_type
     with `enum` (allowed-label lists), so structured output forces the model to pick
     from the gold taxonomy instead of inventing free-form/English labels.
  2. SYSTEM_PROMPT explicitly lists the allowed labels (a second safeguard for models
     without structured output).
  3. The default --prompt-version is "claim-observation-v2-enum" so this run gets a
     fresh experiment_id / cache and does not reuse the non-enum run's cached results.

Rationale: in the non-enum run, claim_class_accuracy and source_scope_exact were 0.0
purely because the model answered in a different label space than the gold set. The
enum constraint removes that artifact so the metrics become meaningful measurements.

rule-based candidate CSV -> HCX structured extraction -> Codex silver comparison.
Each run is isolated by experiment_id and records latency, token usage, estimated
cost, raw prediction, and evaluation metrics.  No API call is made at import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "evaluation" / "pilot120.csv"
DEFAULT_OUTPUT_DIR = ROOT / "output" / "hcx_experiments"
DEFAULT_METRICS_LOG = ROOT / "data" / "hcx_experiment_metrics.jsonl"
DEFAULT_SUMMARY_FILE = ROOT / "data" / "hcx_experiment_summary.csv"
MODEL_DEFAULT = "HCX-003"
RLT_LATENCY_REF_MS_DEFAULT = 6112.722
RLT_TOKENS_REF_DEFAULT = 412.746
MODEL_ALIASES = {
    "HCX-BASH-001": "HCX-DASH-001",
    "HCX-BASH-002": "HCX-DASH-002",
}
V1_MODELS = {"HCX-003", "HCX-DASH-001"}
RESPONSE_FORMAT_MODELS = {"HCX-007"}

# Allowed-label vocabularies. Source of truth: src/retrieval_schema.py
# (CLAIM_CLASSES / SOURCE_SCOPES / SOURCE_ROLES) plus "노이즈", which appears in the
# gold evaluation data as the non-claim class but is absent from that set.
# relation_type mirrors the values enumerated in SYSTEM_PROMPT.
CLAIM_CLASS_ENUM = [
    "집계통계", "개별사례", "전망예측", "목표계획", "법령제도",
    "해석수사", "사고대응임시통계", "여론조사", "통계조사안내", "정정보도", "노이즈",
]
SOURCE_SCOPE_ENUM = ["KOSIS등재", "공식기관_비KOSIS", "민간기관", "해외기관", "불명"]
SOURCE_ROLE_ENUM = ["data_producer", "announcer", "claimer", "comparison_source", "cited_source"]
RELATION_TYPE_ENUM = [
    "time_series", "comparison_pair", "cross_section", "part_whole",
    "numerator_denominator", "multi_indicator", "ranked_list", "range", "untyped",
]

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "is_claim": {"type": "boolean"},
        "claim_class": {"type": "string", "enum": CLAIM_CLASS_ENUM},
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
                    "relation_type": {"type": "string", "enum": RELATION_TYPE_ENUM},
                    "comparison_group": {"type": ["string", "null"]},
                    "sequence": {"type": "integer"},
                },
                "required": ["value", "unit", "period", "relation_type", "comparison_group", "sequence"],
            },
        },
        "source_org_raw": {"type": ["string", "null"]},
        "source_scope": {"type": ["string", "null"], "enum": SOURCE_SCOPE_ENUM + [None]},
        "source_role": {"type": ["string", "null"], "enum": SOURCE_ROLE_ENUM + [None]},
        "evidence_quote": {"type": "string"},
    },
    "required": [
        "is_claim",
        "claim_class",
        "indicator_raw",
        "population",
        "observations",
        "source_org_raw",
        "source_scope",
        "source_role",
        "evidence_quote",
    ],
}

SYSTEM_PROMPT = """
당신은 규칙 기반으로 추출된 후보 문장을 구조화하는 통계 claim 분석기입니다.
입력 문장만 근거로 판단하고, 근거가 없으면 null 또는 빈 배열을 사용합니다.

1. 실제 검증 가능한 주장인지 is_claim으로 판단합니다.
2. 주장이라면 claim_class, indicator_raw, population을 추출합니다.
3. 한 문장에 수치가 여러 개 있으면 claims를 여러 행으로 나누지 말고 observations 배열에 모두 기록합니다.
4. 각 observation은 value, unit, period, time_compare, dimension, relation_type, comparison_group, sequence를 가집니다.
5. relation_type은 time_series, comparison_pair, cross_section, part_whole,
   numerator_denominator, multi_indicator, ranked_list, range, untyped 중 하나입니다.
6. source_org_raw/source_scope/source_role은 문장에 근거가 있을 때만 기록합니다.
7. evidence_quote는 입력 문장에서 직접 인용한 짧은 근거입니다.
8. JSON 객체 하나만 출력합니다.

[허용 라벨 — 아래 목록 밖의 값을 만들어내지 말 것]
- claim_class는 반드시 다음 중 하나: 집계통계, 개별사례, 전망예측, 목표계획, 법령제도, 해석수사, 사고대응임시통계, 여론조사, 통계조사안내, 정정보도, 노이즈.
- source_scope는 반드시 다음 중 하나(근거 없으면 null): KOSIS등재, 공식기관_비KOSIS, 민간기관, 해외기관, 불명.
- source_role은 반드시 다음 중 하나(근거 없으면 null): data_producer, announcer, claimer, comparison_source, cited_source.
- relation_type은 반드시 다음 중 하나: time_series, comparison_pair, cross_section, part_whole, numerator_denominator, multi_indicator, ranked_list, range, untyped.
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


def experiment_id(model: str, temperature: float, top_p: float, max_tokens: int, prompt_version: str) -> str:
    raw = f"{model}|{temperature}|{top_p}|{max_tokens}|{prompt_version}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    safe_model = model.replace("/", "-").replace(" ", "-")
    return f"{safe_model}_t{temperature:g}_p{top_p:g}_{prompt_version}_{digest}"


def parse_content(payload: dict) -> tuple[dict, dict]:
    result = payload.get("result", payload)
    content = result.get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise ValueError("HCX response content is empty")
    text = str(content).strip().lstrip("﻿")
    if text.startswith("```"):
        text = text.replace("```json", "", 1).replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("HCX response does not contain a JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("HCX response JSON is not an object")
    return parsed, result.get("usage", {}) or payload.get("usage", {}) or {}


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
            body["responseFormat"] = {"type": "json", "schema": CLAIM_SCHEMA}
            body["thinking"] = {"effort": "none"}
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


def calculate_metrics(rows: list[dict]) -> dict:
    usable = [row for row in rows if row.get("status") == "ok"]
    gold_bool = [row["gold_is_claim"] for row in usable]
    pred_bool = [row["pred_is_claim"] for row in usable]
    gold_class = [row["gold_claim_class"] for row in usable]
    pred_class = [row["pred_claim_class"] for row in usable]
    class_accuracy = sum(a == b for a, b in zip(gold_class, pred_class)) / len(gold_class) if gold_class else 0.0
    return {
        "rows_total": len(rows),
        "rows_ok": len(usable),
        "rows_error": len(rows) - len(usable),
        "claim_detection": f1_for_binary(gold_bool, pred_bool),
        "claim_class_accuracy": round(class_accuracy, 6),
        "claim_class_macro_f1": macro_f1(gold_class, pred_class),
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY_FILE)
    parser.add_argument("--metrics-log", type=Path, default=DEFAULT_METRICS_LOG)
    parser.add_argument("--model", default=MODEL_DEFAULT)
    parser.add_argument("--api-version", choices=["auto", "v1", "v3"], default="auto")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--max-tokens", type=int, default=1800)
    parser.add_argument("--prompt-version", default="claim-observation-v2-enum")
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
    exp_id = experiment_id(args.model, args.temperature, args.top_p, args.max_tokens, args.prompt_version)
    output_dir = args.output_dir / exp_id
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "cache.jsonl"
    result_path = output_dir / "predictions.csv"
    metrics_path = output_dir / "metrics.json"

    df = pd.read_csv(args.input, keep_default_na=False)
    df = df.iloc[args.offset : args.offset + args.limit if args.limit else None].copy()
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                cache[item["eval_claim_id"]] = item

    api_key = env_api_key()
    rows: list[dict] = []
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
                "prediction_json": json.dumps(prediction, ensure_ascii=False),
            }
            row_result.pop("prediction", None)
            rows.append(row_result)

    output_df = pd.DataFrame(rows)
    output_df.to_csv(result_path, index=False, encoding="utf-8-sig")
    experiment_metadata = {
        "experiment_id": exp_id,
        "model": args.model,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "prompt_version": args.prompt_version,
        "input_file": str(args.input),
        "seed": args.seed,
    }
    metrics = calculate_metrics(rows)
    resolved_version = infer_api_version(args.model) if args.api_version == "auto" else args.api_version
    rlt_weight_total = args.rlt_recall_weight + args.rlt_latency_weight + args.rlt_tokens_weight
    if abs(rlt_weight_total - 1.0) > 1e-9:
        raise ValueError("RLT weights must sum to 1.0")
    metrics.update({**experiment_metadata, "api_version": args.api_version, "resolved_api_version": resolved_version, "effective_temperature": effective_temperature(args.temperature, resolved_version), "latency_ref_ms": args.latency_ref_ms, "tokens_ref": args.tokens_ref, "rlt_recall_weight": args.rlt_recall_weight, "rlt_latency_weight": args.rlt_latency_weight, "rlt_tokens_weight": args.rlt_tokens_weight, "rlt_score": calculate_rlt_score(metrics, args.latency_ref_ms, args.tokens_ref, args.rlt_recall_weight, args.rlt_latency_weight, args.rlt_tokens_weight), "input": str(args.input), "input_cost_per_1m": args.input_cost_per_1m, "output_cost_per_1m": args.output_cost_per_1m})
    update_experiment_summary(metrics, args.summary_file)
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    args.metrics_log.parent.mkdir(parents=True, exist_ok=True)
    with args.metrics_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(metrics, ensure_ascii=False) + "\n")
    print(json.dumps({"experiment_id": exp_id, "predictions": str(result_path), "summary_file": str(args.summary_file), "metrics": str(metrics_path), **metrics}, ensure_ascii=False))


if __name__ == "__main__":
    main()
