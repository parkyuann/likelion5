"""사람이 adjudication할 KOSIS 통계표 검색 gold seed를 만든다.

이 도구는 기존 hybrid 후보를 사람 검토를 빠르게 하기 위한 참고 후보로만
제공한다. 후보 1위나 sample_seed_table_key를 gold로 복사하지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def category_of(claim: dict[str, Any]) -> str:
    """기존 표본의 분류경로 첫 요소로 최소 도메인 층화를 한다."""
    path = str(claim.get("sample_category_path") or "")
    return path.split(">")[0].strip() or "미분류"


def stable_key(claim: dict[str, Any]) -> str:
    return hashlib.sha256(str(claim.get("claim_id") or "").encode("utf-8")).hexdigest()


def retrieval_split(claim: dict[str, Any]) -> str:
    """라벨링 전부터 claim ID 기준 hold-out을 고정해 test 오염을 막는다."""
    return "test" if int(stable_key(claim)[:8], 16) % 5 == 0 else "dev"


def select_claims(claims: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    """도메인을 번갈아 선택해 특정 catalog 경로에만 치우치지 않게 한다."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        if claim.get("is_claim") and claim.get("claim_class") == "집계통계" and claim.get("indicator_raw"):
            groups[category_of(claim)].append(claim)
    for group in groups.values():
        group.sort(key=stable_key)
    selected: list[dict[str, Any]] = []
    offsets = {category: 0 for category in groups}
    categories = sorted(groups)
    while len(selected) < sample_size:
        progressed = False
        for category in categories:
            index = offsets[category]
            if index >= len(groups[category]) or len(selected) >= sample_size:
                continue
            selected.append(groups[category][index])
            offsets[category] += 1
            progressed = True
        if not progressed:
            break
    return selected


def candidate_columns(result: dict[str, Any], count: int) -> dict[str, str]:
    """검토자가 API/웹에서 확인할 수 있게 후보 표 키와 이름만 펼친다."""
    values: dict[str, str] = {}
    candidates = result.get("selected_tables") if isinstance(result, dict) else []
    for rank in range(1, count + 1):
        candidate = candidates[rank - 1] if isinstance(candidates, list) and len(candidates) >= rank else {}
        values[f"candidate_{rank}_table_key"] = str(candidate.get("table_key") or "")
        values[f"candidate_{rank}_table_name"] = str(candidate.get("tbl_name") or "")
    return values


def build_rows(claims: list[dict[str, Any]], hybrid_rows: list[dict[str, Any]], sample_size: int, candidate_count: int) -> list[dict[str, str]]:
    """gold와 참고 후보를 명확히 분리한 검토용 행을 만든다."""
    hybrid_by_claim = {str(row.get("claim_id")): row for row in hybrid_rows}
    selected = select_claims(claims, sample_size)
    rows: list[dict[str, str]] = []
    for index, claim in enumerate(selected, start=1):
        result = hybrid_by_claim.get(str(claim.get("claim_id")), {})
        observations = claim.get("observations") if isinstance(claim.get("observations"), list) else []
        row = {
            "gold_eval_id": f"retrieval_gold_v1_{index:04d}",
            "claim_id": str(claim.get("claim_id") or ""),
            "claim_text": str(claim.get("claim_text") or ""),
            "indicator_raw": str(claim.get("indicator_raw") or ""),
            "population_raw": str(claim.get("population_raw") or ""),
            "source_org_raw": str(claim.get("source_org_raw") or ""),
            "sample_category": category_of(claim),
            "retrieval_split": retrieval_split(claim),
            "observations_json": json.dumps(observations, ensure_ascii=False),
            "sample_seed_table_key_reference_only": str(claim.get("sample_seed_table_key") or ""),
            # 아래 열은 사람이 공식 KOSIS 근거를 확인해 채우는 gold다.
            "gold_match_status": "",
            "gold_table_key": "",
            "gold_org_id": "",
            "gold_tbl_id": "",
            "gold_tbl_name": "",
            "gold_stat_id": "",
            "gold_item_id": "",
            "gold_dimension_json": "",
            "gold_period": "",
            "gold_unit": "",
            "official_evidence_url": "",
            "review_notes": "",
            "review_status": "pending",
            "reviewer": "",
            "reviewed_at": "",
        }
        row.update(candidate_columns(result, candidate_count))
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_guide(path: Path, candidate_count: int) -> None:
    """검토자가 참고 후보를 gold로 오인하지 않도록 최소 adjudication 규칙을 남긴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""# Retrieval Gold v1 라벨링 가이드

이 파일의 후보 1~{candidate_count}위와 `sample_seed_table_key_reference_only`는 **gold가 아니다**. KOSIS 공식 표 화면 또는 OpenAPI 메타데이터를 확인한 뒤에만 결과를 기록한다.

## 허용 상태

- `MATCH`: 하나의 KOSIS 표가 주장의 지표·대상·기간·단위와 부합한다. `gold_table_key`, 공식 근거 URL, 판단 근거를 기록한다.
- `NO_KOSIS_MATCH`: KOSIS에서 검증 가능한 표가 없거나 범위 밖 주장이다. 표 ID를 억지로 채우지 않는다.
- `AMBIGUOUS`: 둘 이상의 표가 동등하게 타당하거나 기사 조건이 부족하다. 후보와 부족한 조건을 notes에 기록한다.
- `SKIP`: 기사 문장 자체가 표 매핑 평가 대상이 아님. 사유를 notes에 기록한다.

`review_status`는 공식 근거 URL과 notes를 확인한 뒤에만 `adjudicated`로 바꾼다. 성능 평가는 `MATCH`이면서 `adjudicated`인 행만 table Recall@K/MRR에 사용하며, `NO_KOSIS_MATCH`는 abstention 평가에만 사용한다.
""", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build human-adjudicated retrieval gold seed")
    parser.add_argument("--claims", type=Path, default=ROOT / "output/hybrid_v3_500_20260723/canonical_claims_500.jsonl")
    parser.add_argument("--hybrid", type=Path, default=ROOT / "output/hybrid_v3_500_20260723/retry_hyde_v3/hybrid_top20_500.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "data/retrieval_gold_v1_annotation_20260724.csv")
    parser.add_argument("--guide", type=Path, default=ROOT / "docs/고도화/retrieval_gold_v1_labeling_guide_20260724.md")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/retrieval_gold_v1_annotation_manifest_20260724.json")
    # eligible claim 수보다 큰 요청은 가능한 행까지만 생성한다. 실제 행 수와
    # split 수는 manifest를 기준으로 G0 gate를 판단한다.
    parser.add_argument("--sample-size", type=int, default=120)
    parser.add_argument("--candidate-count", type=int, default=10)
    args = parser.parse_args()
    rows = build_rows(read_jsonl(args.claims), read_jsonl(args.hybrid), args.sample_size, args.candidate_count)
    if not rows:
        raise ValueError("no eligible aggregate claims with indicators were found")
    write_csv(args.output, rows)
    write_guide(args.guide, args.candidate_count)
    manifest = {
        "requested_sample_size": args.sample_size,
        "rows": len(rows),
        "review_status_counts": {"pending": len(rows)},
        "split_counts": {
            "dev": sum(row["retrieval_split"] == "dev" for row in rows),
            "test": sum(row["retrieval_split"] == "test" for row in rows),
        },
        "gold_rows": 0,
        "candidate_count": args.candidate_count,
        "selection": "round-robin by sample_category, sha256(claim_id) within category",
        "claims_input": str(args.claims),
        "hybrid_input": str(args.hybrid),
        "output": str(args.output),
        "guide": str(args.guide),
        "not_a_retrieval_metric": True,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
