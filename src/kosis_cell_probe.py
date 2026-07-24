"""수집된 KOSIS v4 profile의 메타데이터가 실제 셀 조회로 이어지는지 점검한다.

뉴스 claim의 의미 매핑 전에도 item·dimension·period 코드 계약 자체를 확인할 수
있도록 첫 항목과 최신 수록시점으로 보수적 probe를 만든다. API 키와 원문 응답은
출력 파일에 저장하지 않는다.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

try:
    from .claim_table_aligner import build_cell_query, build_probe_alignment
    from .kosis_client import get_data_from_query
except ImportError:  # pragma: no cover - standalone CLI support
    from claim_table_aligner import build_cell_query, build_probe_alignment
    from kosis_client import get_data_from_query


SAFE_RESPONSE_FIELDS = ("DT", "UNIT_NM", "PRD_DE", "TBL_NM", "ITM_NM", "C1_NM", "C2_NM", "C3_NM")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def probe_profiles(
    profiles: Iterable[dict[str, Any]], *, max_probes: int,
    fetcher: Callable[[dict[str, Any]], list[dict] | dict] = get_data_from_query,
) -> list[dict[str, Any]]:
    """정렬 가능한 profile만 최대 ``max_probes``개 호출하고 감사 레코드를 만든다."""
    records: list[dict[str, Any]] = []
    for profile in profiles:
        if len(records) >= max_probes:
            break
        if profile.get("meta_status") != "enriched":
            continue
        alignment = build_probe_alignment(profile)
        record: dict[str, Any] = {
            "table_key": profile.get("table_key"),
            "align_status": alignment.get("align_status"),
            "alignment_reason": alignment.get("reason"),
        }
        if alignment.get("align_status") != "ALIGNED":
            records.append(record)
            continue
        try:
            query = build_cell_query(profile, alignment)
            response = fetcher(query)
            rows = response if isinstance(response, list) else []
            first = rows[0] if rows else {}
            record.update({
                "query": query,
                "api_status": "OK",
                "response_row_count": len(rows),
                "first_row": {key: first.get(key) for key in SAFE_RESPONSE_FIELDS if key in first},
            })
        except Exception as exc:  # API 장애는 개별 표의 실패로 감사 기록한다.
            record.update({"api_status": "ERROR", "error_type": type(exc).__name__})
            # get_data()가 KOSIS의 JSON 오류 응답을 RuntimeError로 감싼 경우에만
            # 서버가 준 원인 정보를 보존한다. 요청 URL에는 인증키가 들어갈 수 있으므로
            # 네트워크 예외의 문자열은 절대 audit 로그에 기록하지 않는다.
            if isinstance(exc, RuntimeError):
                record["api_error"] = str(exc)
        records.append(record)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="v4 profile의 실제 KOSIS 셀 조회 probe")
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-probes", type=int, default=10)
    args = parser.parse_args()
    if args.max_probes < 1:
        parser.error("--max-probes must be at least 1")

    records = probe_profiles(iter_jsonl(args.profiles), max_probes=args.max_probes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    ok_count = sum(record.get("api_status") == "OK" for record in records)
    aligned_count = sum(record.get("align_status") == "ALIGNED" for record in records)
    print(f"[probe] records={len(records)} aligned={aligned_count} api_ok={ok_count} output={args.output}")


if __name__ == "__main__":
    main()
