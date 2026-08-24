"""독립 실행 가능한 KOSIS OpenAPI v4 catalog crawler.

이 모듈은 현재 작업 환경의 다른 Python 코드나 노트북을 import하지 않는다.
API key는 ``--api-key`` 또는 ``KOSIS_API_KEY`` 환경 변수로만 받고, 산출물과
로그에는 저장하지 않는다. discovery와 metadata enrichment는 모두 checkpoint를
남겨 중단된 데스크톱 실행을 재개할 수 있다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


LIST_URL = "https://kosis.kr/openapi/statisticsList.do"
META_URL = "https://kosis.kr/openapi/statisticsData.do"
META_TYPES = ("TBL", "ITM", "PRD", "SOURCE", "NCD")
TOP_CATEGORIES = (
    ("A", "인구"), ("B", "사회일반"), ("C", "범죄ㆍ안전"), ("D", "노동"),
    ("E", "소득ㆍ소비ㆍ자산"), ("F", "보건"), ("G", "복지"), ("H1", "교육ㆍ훈련"),
    ("H2", "문화ㆍ여가"), ("I1", "주거"), ("I2", "국토이용"), ("J1", "경제일반ㆍ경기"),
    ("J2", "기업경영"), ("K1", "농림"), ("K2", "수산"), ("L", "광업ㆍ제조업"),
    ("M1", "건설"), ("M2", "교통ㆍ물류"), ("N1", "정보통신"), ("N2", "과학ㆍ기술"),
    ("O", "도소매ㆍ서비스"), ("P1", "임금"), ("P2", "물가"), ("Q", "국민계정"),
    ("R", "정부ㆍ재정"), ("S1", "금융"), ("S2", "무역ㆍ국제수지"), ("T", "환경"),
    ("U", "에너지"), ("V", "지역통계"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_hash(value: Any) -> str:
    """원문 전체를 중복 저장하지 않는 NCD 요약에도 변경 탐지 근거를 남긴다."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_value(value: object) -> str:
    """side index의 exact lookup을 위한 보수적 표면형 정규화다."""
    return "".join(str(value or "").strip().lower().split())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # append 중 전원이 끊기면 마지막 JSONL 행만 불완전할 수 있다. 해당
            # endpoint는 checkpoint에 없는 것으로 보고 다음 실행에서 안전하게 재호출한다.
            if number == len(lines) and not text.endswith(("\n", "\r")):
                break
            raise
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number} is not a JSON object")
        rows.append(value)
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    os.replace(temporary, path)


def safe_error_message(error: Exception, api_key: str) -> str:
    """체크포인트·상태 파일에 인증키나 query string이 남지 않게 오류를 축약한다."""
    message = str(error)
    if api_key:
        message = message.replace(api_key, "***")
    message = re.sub(r"(?i)(apiKey=)[^&\s'\"}]+", r"\1***", message)
    return message[:1000]


def read_dotenv_value(path: Path, key: str) -> str:
    """python-dotenv 없이도 일반적인 KEY=value .env 파일을 읽는다."""
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip().removeprefix("export ").strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def get_api_key(argument: str | None, env_file: Path) -> str:
    """명시 인수·환경 변수·동일 디렉터리의 .env 순서로 key를 찾는다."""
    value = (argument or os.getenv("KOSIS_API_KEY") or read_dotenv_value(env_file, "KOSIS_API_KEY")).strip()
    if not value:
        raise ValueError("KOSIS API key is required: set KOSIS_API_KEY or pass --api-key")
    return value


class KosisOpenAPI:
    """KOSIS endpoint 호출을 재시도·지연 정책과 함께 한 곳에 둔다."""

    def __init__(self, api_key: str, *, timeout: float, retries: int, backoff_seconds: float) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.session = requests.Session()

    def _get(self, url: str, params: dict[str, Any], *, method: str) -> list[dict[str, Any]]:
        payload = {"method": method, "apiKey": self.api_key, "format": "json", "jsonVD": "Y", **params}
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("err"):
                    raise RuntimeError(f"KOSIS err={data.get('err')}: {data.get('errMsg')}")
                if not isinstance(data, list):
                    raise TypeError(f"unexpected KOSIS response type: {type(data).__name__}")
                return data
            except Exception as error:  # 실패 유형도 EDA 대상이므로 호출자에게 보존한다.
                last_error = error
                if attempt < self.retries:
                    time.sleep(self.backoff_seconds * (2 ** attempt))
        assert last_error is not None
        raise last_error

    def list_children(self, parent_list_id: str) -> list[dict[str, Any]]:
        return self._get(LIST_URL, {"vwCd": "MT_ZTITLE", "parentListId": parent_list_id}, method="getList")

    def get_meta(self, org_id: str, tbl_id: str, meta_type: str) -> list[dict[str, Any]]:
        # statisticsData.do의 metadata API는 statisticsList와 달리 getMeta를 요구한다.
        return self._get(META_URL, {"orgId": org_id, "tblId": tbl_id, "type": meta_type}, method="getMeta")


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def discover(
    api: KosisOpenAPI,
    *,
    output: Path,
    state_path: Path,
    max_nodes: int | None,
    delay_seconds: float,
    category_ids: set[str] | None,
) -> dict[str, Any]:
    """분류 트리를 BFS로 수집하고 queue/visited checkpoint를 매 노드 갱신한다."""
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not state:
        roots = [(code, name) for code, name in TOP_CATEGORIES if not category_ids or code in category_ids]
        state = {"queue": [{"list_id": code, "path": [name]} for code, name in roots], "visited": []}
    queue: deque[dict[str, Any]] = deque(state.get("queue", []))
    visited = set(str(value) for value in state.get("visited", []))
    seen_tables = {str(row.get("table_key") or "") for row in read_jsonl(output)}
    processed = 0
    new_tables = 0
    while queue and (max_nodes is None or processed < max_nodes):
        node = queue.popleft()
        list_id = str(node["list_id"])
        if list_id in visited:
            continue
        try:
            children = api.list_children(list_id)
        except Exception as error:
            # 실패 노드는 queue 뒤로 보내며 state에 남겨 다음 실행에서 다시 시도한다.
            queue.append(node)
            message = safe_error_message(error, api.api_key)
            save_json(state_path, {"queue": list(queue), "visited": sorted(visited), "last_error": message, "updated_at": utc_now()})
            raise RuntimeError(f"discovery failed at {list_id}: {message}") from error
        visited.add(list_id)
        processed += 1
        for child in children:
            if not isinstance(child, dict):
                continue
            if child.get("TBL_ID") and child.get("ORG_ID"):
                table_key = f"{child['ORG_ID']}:{child['TBL_ID']}"
                if table_key not in seen_tables:
                    append_jsonl(output, {
                        "table_key": table_key,
                        "org_id": str(child["ORG_ID"]),
                        "tbl_id": str(child["TBL_ID"]),
                        "tbl_name": str(child.get("TBL_NM") or ""),
                        "stat_id": str(child.get("STAT_ID") or ""),
                        "category_path": node["path"],
                        "discovered_at": utc_now(),
                    })
                    seen_tables.add(table_key)
                    new_tables += 1
            elif child.get("LIST_ID"):
                queue.append({"list_id": str(child["LIST_ID"]), "path": [*node["path"], str(child.get("LIST_NM") or child["LIST_ID"])]})
        save_json(state_path, {"queue": list(queue), "visited": sorted(visited), "updated_at": utc_now()})
        if delay_seconds:
            time.sleep(delay_seconds)
    return {"processed_nodes": processed, "new_tables": new_tables, "pending_nodes": len(queue), "completed": not queue, "state": str(state_path)}


def canonical_seed(row: dict[str, Any]) -> dict[str, Any]:
    """discovery·gold gap·수동 seed가 같은 enrichment 입력을 쓰도록 통일한다."""
    org_id = str(row.get("org_id") or row.get("ORG_ID") or "")
    tbl_id = str(row.get("tbl_id") or row.get("TBL_ID") or "")
    if not org_id or not tbl_id:
        raise ValueError("seed requires org_id/ORG_ID and tbl_id/TBL_ID")
    expected_table_key = f"{org_id}:{tbl_id}"
    supplied_table_key = str(row.get("table_key") or "")
    if supplied_table_key and supplied_table_key != expected_table_key:
        raise ValueError(f"seed table_key mismatch: expected {expected_table_key}, got {supplied_table_key}")
    raw_path = row.get("category_path") or row.get("path") or []
    category_path = [str(value) for value in raw_path] if isinstance(raw_path, list) else []
    return {
        "table_key": supplied_table_key or expected_table_key,
        "org_id": org_id,
        "tbl_id": tbl_id,
        "tbl_name": str(row.get("tbl_name") or row.get("TBL_NM") or ""),
        "stat_id": str(row.get("stat_id") or row.get("STAT_ID") or ""),
        "category_path": category_path,
        "category_path_status": str(row.get("category_path_status") or ("present" if category_path else "unresolved")),
        "sample_source": str(row.get("sample_source") or ""),
    }


def summarize_ncd(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """NCD 원문은 매우 클 수 있어 최신 갱신일·행수·hash만 catalog에 보존한다."""
    dates = [str(row.get("SEND_DE") or "") for row in rows if isinstance(row, dict)]
    return {"row_count": len(rows), "latest_send_date": max(dates, default="") or None, "response_hash": json_hash(rows)}


def endpoint_record(seed: dict[str, Any], endpoint: str, api: KosisOpenAPI) -> dict[str, Any]:
    """endpoint 하나의 원문/요약과 오류·latency를 API key 없이 기록한다."""
    started = time.perf_counter()
    retrieved_at = utc_now()
    try:
        response = api.get_meta(seed["org_id"], seed["tbl_id"], endpoint)
        stored_response: Any = summarize_ncd(response) if endpoint == "NCD" else response
        return {
            "table_key": seed["table_key"], "org_id": seed["org_id"], "tbl_id": seed["tbl_id"],
            "endpoint": endpoint, "status": "OK", "retrieved_at": retrieved_at,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2), "response": stored_response,
        }
    except Exception as error:
        return {
            "table_key": seed["table_key"], "org_id": seed["org_id"], "tbl_id": seed["tbl_id"],
            "endpoint": endpoint, "status": "ERROR", "retrieved_at": retrieved_at,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error_type": type(error).__name__, "error_message": safe_error_message(error, api.api_key),
        }


def latest_records(raw_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw_rows:
        key = (str(row.get("table_key") or ""), str(row.get("endpoint") or ""))
        if not all(key):
            continue
        previous = result.get(key)
        if previous is None:
            result[key] = row
            continue
        current_time = str(row.get("retrieved_at") or "")
        previous_time = str(previous.get("retrieved_at") or "")
        # 같은 shard를 재개한 append 순서와 여러 checkpoint를 합친 경우 모두에서
        # ISO-8601 조회시점이 더 최신인 record를 선택한다. 과거 파일은 기존 순서를 유지한다.
        if (current_time and (not previous_time or current_time >= previous_time)) or (not current_time and not previous_time):
            result[key] = row
    return result


def split_itm_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """ITM의 ITEM 행과 차원별 value 행을 명시적으로 분리한다."""
    items: list[dict[str, str]] = []
    dimensions: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        obj_id = str(row.get("OBJ_ID") or "")
        itm_id = str(row.get("ITM_ID") or "")
        if not obj_id or not itm_id:
            continue
        if obj_id == "ITEM":
            items.append({"itm_id": itm_id, "itm_nm": str(row.get("ITM_NM") or ""), "unit_nm": str(row.get("UNIT_NM") or "")})
            continue
        dimension = dimensions.setdefault(obj_id, {"obj_id": obj_id, "obj_nm": str(row.get("OBJ_NM") or ""), "values": []})
        dimension["values"].append({"value_id": itm_id, "value_name": str(row.get("ITM_NM") or ""), "value_name_eng": str(row.get("ITM_NM_ENG") or "")})
    return items, list(dimensions.values())


def make_catalog_record(seed: dict[str, Any], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """API 결과를 검색 문서와 셀 정렬 메타데이터가 공존하는 v4 레코드로 만든다."""
    itm_rows = results.get("ITM", {}).get("response", [])
    items, dimensions = split_itm_rows(itm_rows if isinstance(itm_rows, list) else [])
    period_rows = results.get("PRD", {}).get("response", [])
    periods = period_rows if isinstance(period_rows, list) else []
    source_rows = results.get("SOURCE", {}).get("response", [])
    source_rows = source_rows if isinstance(source_rows, list) else []
    table_rows = results.get("TBL", {}).get("response", [])
    table_name = str(table_rows[0].get("TBL_NM") or "") if table_rows else seed["tbl_name"]
    period_types = sorted({str(row.get("PRD_SE") or "") for row in periods if isinstance(row, dict) and row.get("PRD_SE")})
    latest_period = max((str(row.get("END_PRD_DE") or "") for row in periods if isinstance(row, dict)), default="")
    units = sorted({item["unit_nm"] for item in items if item["unit_nm"]})
    source_names = [str(row.get("JOSA_NM") or "") for row in source_rows if isinstance(row, dict) and row.get("JOSA_NM")]
    status = {endpoint: results.get(endpoint, {}).get("status", "NOT_CALLED") for endpoint in META_TYPES}
    readiness_missing: list[str] = []
    if not table_name:
        readiness_missing.append("table_name")
    if not items:
        readiness_missing.append("items")
    if not dimensions:
        readiness_missing.append("dimensions")
    elif any(not dimension.get("values") for dimension in dimensions):
        readiness_missing.append("dimension_values")
    if not periods:
        readiness_missing.append("periods")
    metadata_ready = not readiness_missing
    meta_status = "enriched" if all(value == "OK" for value in status.values()) and metadata_ready else "partial"
    meta_terms = [table_name, *seed["category_path"], *source_names]
    item_terms = [item["itm_nm"] for item in items] + [str(dimension["obj_nm"]) for dimension in dimensions]
    return {
        "table_key": seed["table_key"], "org_id": seed["org_id"], "tbl_id": seed["tbl_id"],
        "tbl_name": table_name, "stat_id": seed["stat_id"] or (str(source_rows[0].get("STAT_ID") or "") if source_rows else ""),
        "category_paths": [seed["category_path"]] if seed["category_path"] else [],
        "category_path_status": str(seed.get("category_path_status") or ("present" if seed["category_path"] else "unresolved")),
        "items": items, "dimensions": dimensions, "units": units, "periods": periods,
        "period_types": period_types, "latest_period": latest_period or None,
        "source_metadata": source_rows, "latest_change": results.get("NCD", {}).get("response", {}).get("latest_send_date"),
        "api_status": status, "meta_status": meta_status,
        "metadata_readiness": {"cell_query_ready": metadata_ready, "missing": readiness_missing},
        # 검색 문서는 값 목록을 넣지 않는다. 고카디널리티 값은 별도 side index가 맡는다.
        "doc_meta_text": " | ".join(term for term in meta_terms if term),
        "doc_item_index": " ".join(term for term in item_terms if term),
        "catalog_version": "kosis-api-catalog-v4", "value_parse_status": "api_structured",
        "sample_source": seed["sample_source"], "retrieved_at": utc_now(),
    }


def make_value_side_index(record: dict[str, Any]) -> list[dict[str, str]]:
    """모든 API 차원값을 dense/BM25 문서와 분리한 exact-value side index로 만든다."""
    rows: list[dict[str, str]] = []
    for dimension in record.get("dimensions", []):
        for value in dimension.get("values", []):
            name = str(value.get("value_name") or "")
            if name:
                rows.append({
                    "normalized_value": normalize_value(name), "surface_form": name,
                    "table_key": str(record["table_key"]), "obj_id": str(dimension.get("obj_id") or ""),
                    "obj_nm": str(dimension.get("obj_nm") or ""), "value_id": str(value.get("value_id") or ""),
                })
    return rows


def enrich(
    api: KosisOpenAPI,
    *,
    seeds_path: Path,
    raw_output: Path,
    catalog_output: Path,
    value_index_output: Path,
    manifest_output: Path,
    retry_errors: bool,
    delay_seconds: float,
    max_tables: int | None,
    show_progress: bool = True,
) -> dict[str, Any]:
    """seed 표의 API 메타데이터를 수집해 v4 catalog와 value side index를 만든다."""
    seeds = [canonical_seed(row) for row in read_jsonl(seeds_path)]
    duplicate_seed_keys = sorted(key for key, count in Counter(seed["table_key"] for seed in seeds).items() if count > 1)
    if duplicate_seed_keys:
        raise ValueError(f"duplicate seed table_key values: {', '.join(duplicate_seed_keys[:10])}")
    if max_tables is not None:
        seeds = seeds[:max_tables]
    cached = latest_records(read_jsonl(raw_output))
    profiles: list[dict[str, Any]] = []
    new_records: list[dict[str, Any]] = []
    total_tables = len(seeds)
    started = time.perf_counter()
    if show_progress:
        cached_ok = sum(1 for record in cached.values() if record.get("status") == "OK")
        print(f"[crawl] started: {total_tables} tables; reusable successful checkpoints: {cached_ok}", flush=True)
    for table_number, seed in enumerate(seeds, start=1):
        if show_progress:
            print(f"[crawl] working on table {table_number}/{total_tables}: {seed['table_key']}", flush=True)
        results: dict[str, dict[str, Any]] = {}
        for endpoint in META_TYPES:
            prior = cached.get((seed["table_key"], endpoint))
            if prior and (prior.get("status") == "OK" or not retry_errors):
                results[endpoint] = prior
                continue
            record = endpoint_record(seed, endpoint, api)
            append_jsonl(raw_output, record)
            results[endpoint] = record
            new_records.append(record)
            if delay_seconds:
                time.sleep(delay_seconds)
        profiles.append(make_catalog_record(seed, results))
        if show_progress:
            enriched = sum(profile["meta_status"] == "enriched" for profile in profiles)
            percent = round((table_number / total_tables * 100) if total_tables else 100, 1)
            elapsed = round(time.perf_counter() - started, 1)
            print(f"[crawl] {table_number}/{total_tables} tables ({percent}%) | enriched {enriched} | "
                  f"new API calls {len(new_records)} | elapsed {elapsed}s", flush=True)
    write_jsonl(catalog_output, profiles)
    side_rows = [row for profile in profiles for row in make_value_side_index(profile)]
    write_jsonl(value_index_output, side_rows)
    endpoint_status = Counter(f"{row['endpoint']}:{row['status']}" for row in new_records)
    manifest = {
        "generated_at": utc_now(), "input_seeds": len(seeds), "new_api_calls": len(new_records),
        "input_seed_sha256": json_hash(seeds),
        "endpoint_status_counts": dict(sorted(endpoint_status.items())),
        "catalog_status_counts": dict(sorted(Counter(profile["meta_status"] for profile in profiles).items())),
        "value_side_index_rows": len(side_rows), "catalog_version": "kosis-api-catalog-v4",
        "raw_output": str(raw_output), "catalog_output": str(catalog_output), "value_index_output": str(value_index_output),
        "api_key_persisted": False,
    }
    save_json(manifest_output, manifest)
    return manifest


def crawl_progress(*, seeds_path: Path, raw_output: Path) -> dict[str, Any]:
    """Return the current checkpoint-based enrichment progress without calling KOSIS."""
    seeds = [canonical_seed(row) for row in read_jsonl(seeds_path)]
    latest = latest_records(read_jsonl(raw_output))
    endpoint_counts: dict[str, dict[str, int]] = {}
    table_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()

    for endpoint in META_TYPES:
        counts: Counter[str] = Counter()
        for seed in seeds:
            record = latest.get((seed["table_key"], endpoint))
            counts[str(record.get("status") if record else "NOT_STARTED")] += 1
        endpoint_counts[endpoint] = dict(sorted(counts.items()))

    for seed in seeds:
        statuses = [str(latest.get((seed["table_key"], endpoint), {}).get("status") or "NOT_STARTED") for endpoint in META_TYPES]
        if all(status == "OK" for status in statuses):
            table_counts["enriched"] += 1
        elif any(status == "ERROR" for status in statuses):
            table_counts["error"] += 1
            for status, endpoint in zip(statuses, META_TYPES):
                if status == "ERROR":
                    error_counts[endpoint] += 1
        elif any(status != "NOT_STARTED" for status in statuses):
            table_counts["partial"] += 1
        else:
            table_counts["not_started"] += 1

    total_tables = len(seeds)
    total_expected_calls = total_tables * len(META_TYPES)
    completed_calls = sum(counts.get("OK", 0) for counts in endpoint_counts.values())
    return {
        "checked_at": utc_now(),
        "seeds_path": str(seeds_path),
        "raw_output": str(raw_output),
        "tables": {
            "total": total_tables,
            "enriched": table_counts["enriched"],
            "partial": table_counts["partial"],
            "error": table_counts["error"],
            "not_started": table_counts["not_started"],
            "completion_percent": round((table_counts["enriched"] / total_tables * 100) if total_tables else 0, 1),
        },
        "api_calls": {
            "expected": total_expected_calls,
            "ok": completed_calls,
            "completion_percent": round((completed_calls / total_expected_calls * 100) if total_expected_calls else 0, 1),
        },
        "endpoint_status_counts": endpoint_counts,
        "error_endpoint_counts": dict(sorted(error_counts.items())),
    }


def print_crawl_progress(progress: dict[str, Any]) -> None:
    """Print a compact, human-readable form of :func:`crawl_progress`."""
    tables = progress["tables"]
    calls = progress["api_calls"]
    print("KOSIS catalog crawl progress")
    print(f"Tables: {tables['enriched']}/{tables['total']} enriched ({tables['completion_percent']}%) | "
          f"partial {tables['partial']} | errors {tables['error']} | not started {tables['not_started']}")
    print(f"API calls: {calls['ok']}/{calls['expected']} OK ({calls['completion_percent']}%)")
    for endpoint in META_TYPES:
        counts = progress["endpoint_status_counts"][endpoint]
        print(f"  {endpoint}: OK {counts.get('OK', 0)} | ERROR {counts.get('ERROR', 0)} | "
              f"NOT_STARTED {counts.get('NOT_STARTED', 0)}")
    if progress["error_endpoint_counts"]:
        print("Retry errors with: enrich ... --retry-errors")


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone KOSIS OpenAPI catalog crawler")
    parser.add_argument("--api-key", help="otherwise read KOSIS_API_KEY from the environment")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="fallback .env path (default: .env)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--backoff-seconds", type=float, default=1.0)
    commands = parser.add_subparsers(dest="command", required=True)

    discover_parser = commands.add_parser("discover", help="resume-safe KOSIS category tree discovery")
    discover_parser.add_argument("--output", type=Path, required=True)
    discover_parser.add_argument("--state", type=Path, required=True)
    discover_parser.add_argument("--max-nodes", type=int)
    discover_parser.add_argument("--delay-seconds", type=float, default=0.15)
    discover_parser.add_argument("--category-id", action="append", help="repeat to limit initial discovery to selected top categories")

    enrich_parser = commands.add_parser("enrich", help="crawl API metadata into a v4 catalog")
    enrich_parser.add_argument("--seeds", type=Path, required=True)
    enrich_parser.add_argument("--raw-output", type=Path, required=True)
    enrich_parser.add_argument("--catalog-output", type=Path, required=True)
    enrich_parser.add_argument("--value-index-output", type=Path, required=True)
    enrich_parser.add_argument("--manifest", type=Path, required=True)
    enrich_parser.add_argument("--retry-errors", action="store_true")
    enrich_parser.add_argument("--delay-seconds", type=float, default=0.2)
    enrich_parser.add_argument("--max-tables", type=int)
    enrich_parser.add_argument("--quiet", action="store_true", help="suppress live crawl progress lines")

    status_parser = commands.add_parser("status", help="show checkpoint-based crawl progress; does not call KOSIS")
    status_parser.add_argument("--seeds", type=Path, required=True)
    status_parser.add_argument("--raw-output", type=Path, required=True)
    status_parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a summary")

    args = parser.parse_args()
    if args.command == "status":
        result = crawl_progress(seeds_path=args.seeds, raw_output=args.raw_output)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_crawl_progress(result)
        return

    api = KosisOpenAPI(get_api_key(args.api_key, args.env_file), timeout=args.timeout, retries=args.retries, backoff_seconds=args.backoff_seconds)
    if args.command == "discover":
        result = discover(api, output=args.output, state_path=args.state, max_nodes=args.max_nodes,
                          delay_seconds=args.delay_seconds, category_ids=set(args.category_id) if args.category_id else None)
    else:
        result = enrich(api, seeds_path=args.seeds, raw_output=args.raw_output, catalog_output=args.catalog_output,
                        value_index_output=args.value_index_output, manifest_output=args.manifest,
                        retry_errors=args.retry_errors, delay_seconds=args.delay_seconds, max_tables=args.max_tables,
                        show_progress=not args.quiet)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
