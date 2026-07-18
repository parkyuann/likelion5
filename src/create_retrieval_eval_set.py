"""claim_listform.csv에서 KOSIS 검색 평가용 claim 표본을 만든다.

이 스크립트는 정답 표를 자동으로 추측하지 않는다. 선택된 행에 사람이
KOSIS 표/분류코드/시점 gold를 기록할 수 있는 빈 annotation 컬럼을 추가한다.
기사 단위 분할을 위해 표본에서는 article_idx를 중복시키지 않는다.

실행 (레포 루트):
    venv/Scripts/python.exe src/create_retrieval_eval_set.py
"""

from __future__ import annotations

import json
import argparse
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH = ROOT / "data" / "claim_listform.csv"
OUTPUT_PATH = ROOT / "data" / "retrieval_eval_claims_v0.csv"
MANIFEST_PATH = ROOT / "data" / "retrieval_eval_claims_v0_manifest.json"
SEED = 20260715
DEFAULT_FRACTION = 0.8

# 상태별로 희소한 나열형 실패 케이스를 의도적으로 포함한다.
STATUS_QUOTAS = {
    "ALIGNED": 15,
    "COUNT_MISMATCH": 10,
    "LOW_CONFIDENCE": 10,
    "NOT_LIST_FORM": 40,
    "SINGLE_VALUE": 45,
}

ANNOTATION_COLUMNS = {
    "gold_claim_class": "",
    "gold_source_scope": "",
    "gold_verifiability_prefilter": "",
    "gold_org_id": "",
    "gold_org_name": "",
    "gold_source_role": "",
    "gold_tbl_id": "",
    "gold_tbl_name": "",
    "gold_stat_id": "",
    "gold_dimension_json": "",
    "gold_item_id": "",
    "gold_period_type": "",
    "gold_period": "",
    "gold_unit": "",
    "gold_match_status": "",
    "gold_notes": "",
    "review_status": "pending",
    "reviewer": "",
}


def choose_one_per_article(group: pd.DataFrame, random_state: int) -> pd.DataFrame:
    """같은 기사에서 여러 후보가 나오더라도 하나만 남긴다."""
    shuffled = group.sample(frac=1, random_state=random_state)
    return shuffled.drop_duplicates(subset=["article_idx"], keep="first")


def choose_status_rows(
    group: pd.DataFrame,
    quota: int,
    random_state: int,
    excluded_articles: set[int] | None = None,
    one_per_article: bool = False,
) -> pd.DataFrame:
    excluded_articles = excluded_articles or set()
    group = group[~group["article_idx"].isin(excluded_articles)]
    available = choose_one_per_article(group, random_state) if one_per_article else group.sample(
        frac=1, random_state=random_state
    )
    source = available[available["source_mentioned"].astype(str).str.lower() == "true"]
    non_source = available[~available.index.isin(source.index)]
    source_quota = min(max(1, quota // 5), len(source)) if quota else 0
    selected_source = source.sample(n=source_quota, random_state=random_state) if source_quota else source.iloc[0:0]
    remaining = quota - len(selected_source)
    if remaining > len(non_source):
        remaining = len(non_source)
    selected_other = non_source.sample(n=remaining, random_state=random_state + 1) if remaining else non_source.iloc[0:0]
    selected = pd.concat([selected_source, selected_other])
    if len(selected) < quota:
        used = set(selected.index)
        fallback = available[~available.index.isin(used)]
        selected = pd.concat([selected, fallback.head(quota - len(selected))])
    return selected


def assign_split(article_idx: int) -> str:
    # 기사 단위 hold-out: 같은 article_idx는 항상 같은 split으로 간다.
    return "test" if int(article_idx) % 5 == 0 else "dev"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fraction", type=float, default=DEFAULT_FRACTION)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--one-per-article", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    if not 0 < args.fraction <= 1:
        raise ValueError("--fraction은 0보다 크고 1 이하여야 합니다.")

    df = pd.read_csv(INPUT_PATH).reset_index(names="source_row_number")
    if "article_idx" not in df or "list_alignment_status" not in df:
        raise ValueError("claim_listform.csv에 article_idx/list_alignment_status가 필요합니다.")

    target_rows = round(len(df) * args.fraction)
    status_counts = df["list_alignment_status"].value_counts().to_dict()
    raw_quotas = {status: status_counts.get(status, 0) * args.fraction for status in STATUS_QUOTAS}
    quotas = {status: int(raw_quotas[status]) for status in raw_quotas}
    remainder = target_rows - sum(quotas.values())
    for status in sorted(raw_quotas, key=lambda key: raw_quotas[key] - quotas[key], reverse=True):
        if remainder <= 0:
            break
        quotas[status] += 1
        remainder -= 1

    selected_parts = []
    used_articles: set[int] = set()
    for offset, status in enumerate(STATUS_QUOTAS):
        quota = quotas[status]
        group = df[df["list_alignment_status"] == status]
        if len(group) < quota:
            raise ValueError(f"{status}: {quota}건이 필요하지만 {len(group)}건뿐입니다.")
        selected = choose_status_rows(
            group,
            quota,
            args.seed + offset * 100,
            used_articles if args.one_per_article else None,
            one_per_article=args.one_per_article,
        )
        if len(selected) < quota:
            raise ValueError(f"{status}: 기존 선택 기사와 겹치지 않는 {quota}건을 만들 수 없습니다.")
        selected_parts.append(selected)
        if args.one_per_article:
            used_articles.update(selected["article_idx"].astype(int))

    result = pd.concat(selected_parts, ignore_index=True)
    if args.one_per_article and result["article_idx"].duplicated().any():
        raise ValueError("표본 생성 결과에 article_idx 중복이 있습니다.")

    result = result.sample(frac=1, random_state=args.seed).reset_index(drop=True)
    result.insert(0, "eval_claim_id", [f"eval_{i:04d}" for i in range(1, len(result) + 1)])
    result.insert(2, "retrieval_split", result["article_idx"].map(assign_split))
    for column, default in ANNOTATION_COLUMNS.items():
        result[column] = default

    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False, encoding="utf-8-sig")

    manifest = {
        "input": str(INPUT_PATH.relative_to(ROOT)),
        "output": str(args.output.relative_to(ROOT)),
        "seed": args.seed,
        "fraction": args.fraction,
        "selection_unit": "one claim row per article_idx" if args.one_per_article else "claim row",
        "status_quotas": quotas,
        "rows": len(result),
        "articles": int(result["article_idx"].nunique()),
        "split_counts": result["retrieval_split"].value_counts().to_dict(),
        "status_counts": result["list_alignment_status"].value_counts().to_dict(),
        "annotation_columns": list(ANNOTATION_COLUMNS),
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"표본 {len(result)}건 생성: {args.output}")
    print(f"상태 분포: {manifest['status_counts']}")
    print(f"분할: {manifest['split_counts']}")


if __name__ == "__main__":
    main()
