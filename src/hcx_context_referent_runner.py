"""문맥 referent fixture를 HCX로 판정하되, 결과를 사람 검토 전에는 적용하지 않는다."""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any

try:
    from .context_referent_adjudication import validate_adjudication
    from .hcx_claim_experiment import call_hcx, env_api_key
except ImportError:  # pragma: no cover - standalone CLI support
    from context_referent_adjudication import validate_adjudication
    from hcx_claim_experiment import call_hcx, env_api_key


SYSTEM_PROMPT = """
당신은 뉴스 claim의 지시 대상을 판정하는 검토기다. 입력 JSON의 claim_text, context_window,
candidate_terms, evidence만 사용한다. 후보에 없는 보험명·기관명·상품명을 새로 만들지 않는다.

출력은 JSON 객체 하나다.
{
  "adjudication_status": "RESOLVED" | "AMBIGUOUS" | "NO_CONTEXT",
  "selected_referent": "RESOLVED일 때 candidate_terms 중 정확히 하나, 아니면 빈 문자열",
  "evidence_sentence_index": "RESOLVED일 때 그 후보가 evidence에 있는 문장 index, 아니면 null",
  "adjudication_notes": "짧은 한국어 근거"
}

RESOLVED는 기사 문맥이 claim의 대상을 후보 하나로 명확히 한정할 때만 사용한다. 단순히 가까운
문장에 후보 하나가 있다는 이유만으로 추정하지 않는다. 불명확하면 AMBIGUOUS, 근거가 없으면
NO_CONTEXT를 사용한다.
""".strip()


def decode_list(value: str) -> list[Any]:
    parsed = json.loads(value or "[]")
    if not isinstance(parsed, list):
        raise ValueError("fixture JSON value must be an array")
    return parsed


def hcx_input(row: dict[str, str]) -> tuple[dict[str, Any], str]:
    fixture = {
        "context_eval_id": row["context_eval_id"],
        "candidate_terms": decode_list(row.get("candidate_terms_json", "[]")),
        "evidence": decode_list(row.get("evidence_json", "[]")),
    }
    payload = {
        "claim_text": row.get("claim_text", ""), "article_title": row.get("article_title", ""),
        "context_window": decode_list(row.get("context_window_json", "[]")),
        "candidate_terms": fixture["candidate_terms"], "evidence": fixture["evidence"],
    }
    return fixture, json.dumps(payload, ensure_ascii=False)


def normalize_prediction(prediction: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    decision = {
        "adjudication_status": str(prediction.get("adjudication_status") or ""),
        "selected_referent": str(prediction.get("selected_referent") or ""),
        "evidence_sentence_index": prediction.get("evidence_sentence_index"),
        "adjudication_source": "HCX",
    }
    if isinstance(decision["evidence_sentence_index"], str) and decision["evidence_sentence_index"].strip():
        try:
            decision["evidence_sentence_index"] = int(decision["evidence_sentence_index"])
        except ValueError:
            pass
    errors = validate_adjudication(fixture, decision)
    if errors:
        raise ValueError("invalid HCX context decision: " + "; ".join(errors))
    return {
        "adjudication_status": decision["adjudication_status"],
        "selected_referent": decision["selected_referent"],
        "evidence_sentence_index": decision["evidence_sentence_index"],
        "adjudication_notes": str(prediction.get("adjudication_notes") or ""),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def compact_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    """동일 fixture ID의 중복 실행 기록은 마지막 결과 하나로 압축한다."""
    rows = read_jsonl(path)
    latest = {str(row.get("context_eval_id")): row for row in rows if row.get("context_eval_id")}
    if len(latest) != len(rows):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in latest.values()), encoding="utf-8")
        os.replace(temporary, path)
    return latest


def acquire_lock(output: Path) -> Path:
    lock = output.with_suffix(output.suffix + ".lock")
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError as error:
        raise RuntimeError(f"HCX context batch is already running: {lock}")
    return lock


def run_rows(
    rows: list[dict[str, str]], *, output: Path, api_key: str, model: str, max_rows: int | None,
    delay_seconds: float, call=call_hcx,
) -> list[dict[str, Any]]:
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(output)
    try:
        prior = compact_checkpoint(output)
        pending = [row for row in rows if row["context_eval_id"] not in prior]
        if max_rows is not None:
            pending = pending[:max_rows]
        results: list[dict[str, Any]] = []
        for number, row in enumerate(pending, start=1):
            fixture, user_input = hcx_input(row)
            started = time.perf_counter()
            try:
                prediction, usage, latency_ms = call(
                    user_input, api_key, model, 0.0, 0.8, 350, SYSTEM_PROMPT,
                    api_version="auto", use_response_format=False, timeout=120,
                )
                normalized = normalize_prediction(prediction, fixture)
                result = {
                    "context_eval_id": row["context_eval_id"], "status": "ok", "model": model,
                    "latency_ms": round(latency_ms, 2), "usage": usage, "prediction": normalized,
                }
            except Exception as error:
                result = {
                    "context_eval_id": row["context_eval_id"], "status": "error", "model": model,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error_type": type(error).__name__, "error": str(error)[:1000],
                }
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            results.append(result)
            print(f"[hcx-context] {number}/{len(pending)} {row['context_eval_id']} {result['status']}", flush=True)
            if delay_seconds:
                time.sleep(delay_seconds)
        return results
    finally:
        lock.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="HCX 문맥 referent batch runner")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="HCX-007")
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    args = parser.parse_args()
    with args.fixture.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    api_key = env_api_key()
    if not api_key:
        raise RuntimeError("HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY가 필요합니다.")
    results = run_rows(rows, output=args.output, api_key=api_key, model=args.model, max_rows=args.max_rows, delay_seconds=args.delay_seconds)
    print(json.dumps({"new_rows": len(results), "ok": sum(row["status"] == "ok" for row in results),
                      "error": sum(row["status"] == "error" for row in results), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
