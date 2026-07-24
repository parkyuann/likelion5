"""KOSIS OpenAPI 통계표의 실제 메타데이터를 수집·정규화하는 EDA 도구.

catalog의 평문 설명이 아니라 KOSIS API 응답을 기준으로 표의 항목, 차원값,
기간, 조사 정보, 갱신 이력을 기록한다. API key와 요청 URL은 산출물에 쓰지
않으며, endpoint 하나의 실패가 표 전체 수집을 중단시키지 않도록 설계했다.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


# UNIT endpoint는 일부 실제 표에서 데이터 없음(err=30)을 반환했다. 항목/셀 단위가
# 더 신뢰할 수 있으므로 EDA의 필수 메타데이터 요청에서는 의도적으로 제외한다.
META_ENDPOINTS = ("TBL", "ITM", "PRD", "SOURCE", "NCD")
GetMeta = Callable[[str, str, str], list[dict[str, Any]]]


def utc_now() -> str:
    """산출물 간 비교가 가능하도록 API 조회 시각을 UTC ISO 형식으로 만든다."""
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """빈 줄을 무시하고 JSONL 객체를 읽는다."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """정규화 산출물을 UTF-8 JSONL로 다시 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """API 결과는 호출 직후 append해 중단 뒤 resume 가능한 checkpoint로 남긴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def normalize_seed(row: dict[str, Any]) -> dict[str, str]:
    """tree/catalog/search 어느 입력이든 API 호출에 필요한 표 식별자로 통일한다."""
    org_id = str(row.get("org_id") or row.get("ORG_ID") or "")
    tbl_id = str(row.get("tbl_id") or row.get("TBL_ID") or "")
    if not org_id or not tbl_id:
        raise ValueError("each table seed requires org_id/ORG_ID and tbl_id/TBL_ID")
    return {
        "table_key": str(row.get("table_key") or f"{org_id}:{tbl_id}"),
        "org_id": org_id,
        "tbl_id": tbl_id,
        "tbl_name": str(row.get("tbl_name") or row.get("TBL_NM") or ""),
        "sample_source": str(row.get("sample_source") or row.get("source") or ""),
    }


def default_get_meta(org_id: str, tbl_id: str, meta_type: str) -> list[dict[str, Any]]:
    """실행 시에만 기존 client를 불러 테스트가 외부 API 설정에 의존하지 않게 한다."""
    try:
        from .kosis_client import get_meta
    except ImportError:
        from kosis_client import get_meta
    return get_meta(org_id, tbl_id, meta_type)


def collect_endpoint(
    seed: dict[str, str],
    endpoint: str,
    get_meta_fn: GetMeta,
) -> dict[str, Any]:
    """endpoint 단위로 성공·실패·지연시간을 같은 구조로 보존한다."""
    started = time.perf_counter()
    retrieved_at = utc_now()
    try:
        response = get_meta_fn(seed["org_id"], seed["tbl_id"], endpoint)
        if not isinstance(response, list):
            raise TypeError(f"expected list response, got {type(response).__name__}")
        return {
            **seed,
            "endpoint": endpoint,
            "status": "OK",
            "retrieved_at": retrieved_at,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "response": response,
        }
    except Exception as error:  # API error는 표본 분포의 일부이므로 계속 수집한다.
        return {
            **seed,
            "endpoint": endpoint,
            "status": "ERROR",
            "retrieved_at": retrieved_at,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error_type": type(error).__name__,
            "error_message": str(error),
        }


def latest_endpoint_records(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """resume 시 같은 표·endpoint의 가장 최근 결과를 사용한다."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        table_key = str(row.get("table_key") or "")
        endpoint = str(row.get("endpoint") or "")
        if table_key and endpoint:
            latest[(table_key, endpoint)] = row
    return latest


def build_profile(seed: dict[str, str], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """ITM의 항목 행과 차원값 행을 분리해 셀 정렬이 가능한 profile로 만든다."""
    itm_rows = results.get("ITM", {}).get("response", [])
    items: list[dict[str, Any]] = []
    dimensions: dict[str, dict[str, Any]] = {}
    for row in itm_rows if isinstance(itm_rows, list) else []:
        if not isinstance(row, dict):
            continue
        obj_id = str(row.get("OBJ_ID") or "")
        itm_id = str(row.get("ITM_ID") or "")
        itm_nm = str(row.get("ITM_NM") or "")
        if not obj_id or not itm_id:
            continue
        if obj_id == "ITEM":
            items.append({
                "item_id": itm_id,
                "item_name": itm_nm,
                "unit_name": str(row.get("UNIT_NM") or ""),
                "item_name_eng": str(row.get("ITM_NM_ENG") or ""),
            })
            continue
        dimension = dimensions.setdefault(obj_id, {
            "dimension_id": obj_id,
            "dimension_name": str(row.get("OBJ_NM") or ""),
            "values": [],
        })
        dimension["values"].append({
            "value_id": itm_id,
            "value_name": itm_nm,
            "value_name_eng": str(row.get("ITM_NM_ENG") or ""),
        })

    periods = results.get("PRD", {}).get("response", [])
    sources = results.get("SOURCE", {}).get("response", [])
    changes = results.get("NCD", {}).get("response", [])
    table_rows = results.get("TBL", {}).get("response", [])
    latest_change = max(
        (str(row.get("SEND_DE") or "") for row in changes if isinstance(row, dict)),
        default="",
    )
    return {
        **seed,
        "api_status": {endpoint: results.get(endpoint, {}).get("status", "NOT_CALLED") for endpoint in META_ENDPOINTS},
        "api_table_name": str(table_rows[0].get("TBL_NM") or "") if table_rows else "",
        "items": items,
        "dimensions": list(dimensions.values()),
        "periods": periods if isinstance(periods, list) else [],
        "sources": sources if isinstance(sources, list) else [],
        "latest_change_date": latest_change or None,
        "profile_status": "READY" if items and dimensions and results.get("PRD", {}).get("status") == "OK" else "INCOMPLETE",
    }


def collect_profiles(
    seeds: Iterable[dict[str, Any]],
    *,
    raw_output: Path,
    profile_output: Path,
    manifest_output: Path,
    get_meta_fn: GetMeta = default_get_meta,
    resume: bool = True,
    pause_seconds: float = 0.0,
) -> dict[str, Any]:
    """표본 전체를 수집하고 profile·manifest를 생성한다.

    기존 raw checkpoint에 성공 결과가 있으면 다시 호출하지 않는다. 오류 결과는 API
    일시 장애가 회복됐는지 확인할 수 있도록 재실행 때 다시 호출한다.
    """
    cached = latest_endpoint_records(read_jsonl(raw_output)) if resume else {}
    profiles: list[dict[str, Any]] = []
    new_records: list[dict[str, Any]] = []
    for raw_seed in seeds:
        seed = normalize_seed(raw_seed)
        results: dict[str, dict[str, Any]] = {}
        for endpoint in META_ENDPOINTS:
            prior = cached.get((seed["table_key"], endpoint))
            if prior and prior.get("status") == "OK":
                results[endpoint] = prior
                continue
            record = collect_endpoint(seed, endpoint, get_meta_fn)
            append_jsonl(raw_output, record)
            new_records.append(record)
            results[endpoint] = record
            if pause_seconds:
                time.sleep(pause_seconds)
        profiles.append(build_profile(seed, results))
    write_jsonl(profile_output, profiles)

    endpoint_status = Counter(
        f"{record['endpoint']}:{record['status']}" for record in new_records
    )
    manifest = {
        "generated_at": utc_now(),
        "input_tables": len(profiles),
        "new_api_calls": len(new_records),
        "reused_successful_calls": len(profiles) * len(META_ENDPOINTS) - len(new_records),
        "endpoint_status_counts": dict(sorted(endpoint_status.items())),
        "profile_status_counts": dict(sorted(Counter(profile["profile_status"] for profile in profiles).items())),
        "raw_output": str(raw_output),
        "profile_output": str(profile_output),
        "api_key_persisted": False,
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="KOSIS OpenAPI metadata EDA collector")
    parser.add_argument("--input", type=Path, required=True, help="org_id/tbl_id seed JSONL")
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--profile-output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pause-seconds", type=float, default=0.2)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    manifest = collect_profiles(
        read_jsonl(args.input),
        raw_output=args.raw_output,
        profile_output=args.profile_output,
        manifest_output=args.manifest,
        resume=not args.no_resume,
        pause_seconds=args.pause_seconds,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
