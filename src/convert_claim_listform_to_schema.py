"""claim_listform.csv를 Claim v1 JSONL과 gold Mapping JSONL로 변환한다.

자동으로 확정할 수 없는 모집단·기관 orgId·KOSIS 표는 추측하지 않는다.
현재 입력에 값이 하나만 있으면 수치/단위를 정규화하고, 여러 값이 남아 있으면
원본 목록을 보존한 채 정규화 값은 비워 사람 또는 후속 정렬기가 처리하게 한다.

실행:
    venv/Scripts/python.exe src/convert_claim_listform_to_schema.py
    venv/Scripts/python.exe src/convert_claim_listform_to_schema.py \
      --input data/retrieval_eval_claims_v0.csv \
      --output data/retrieval_eval_claims_v1.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from retrieval_schema import (
    Attribution,
    Claim,
    ClaimObservation,
    ClaimTableMapping,
    classify_observation_verify_scope,
    validate_claim,
    validate_mapping,
)
from claim_normalizer import parse_korean_number, resolve_relative_time


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "claim_listform.csv"
DEFAULT_OUTPUT = ROOT / "data" / "claims_v1.jsonl"

YEAR_MONTH_RE = re.compile(r"(?P<year>19\d{2}|20\d{2})[년.\-/ ]+(?P<month>\d{1,2})월?")
YEAR_RE = re.compile(r"(?P<year>19\d{2}|20\d{2})년?")
QUARTER_RE = re.compile(r"(?P<quarter>[1-4])분기")
DATE_RE = re.compile(r"(?P<year>19\d{2}|20\d{2})년\s*(?P<month>\d{1,2})월\s*(?P<day>\d{1,2})일")

# 수사 전개(조/억/만/천, 중첩 계수, 음수 부호)는 claim_normalizer.parse_korean_number로 통일한다.
# 그 함수는 '62조9천444억5700만'(중첩)·'-7.6'(감소 부호)까지 처리하고, 범위('19~20')는 None을
# 돌려주므로 관측값(observation) 수치의 정확도·부호가 그대로 보존된다.
UNIT_MAP = {
    "천명": ("명", 1_000), "만명": ("명", 10_000),
    "천원": ("원", 1_000), "만원": ("원", 10_000),
    "억원": ("원", 100_000_000), "조원": ("원", 1_000_000_000_000),
}


def clean(value) -> str | None:
    text = str(value).strip()
    return text if text and text.lower() not in {"nan", "none", "null"} else None


def split_list(value) -> list[str]:
    text = clean(value)
    return [part.strip() for part in text.split(";")] if text else []


def json_object(value) -> dict:
    """Parse extractor JSON without allowing malformed metadata to break rows."""
    text = clean(value)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_value(raw_value: str | None, raw_unit: str | None) -> tuple[float | None, str | None]:
    unit = clean(raw_unit)
    if not unit:
        return None, None
    unit = unit.replace(" ", "")
    canonical_unit, unit_multiplier = UNIT_MAP.get(unit, (unit, 1))
    number = parse_korean_number(clean(raw_value))
    return (number * unit_multiplier if number is not None else None), canonical_unit


def normalize_time(raw_time: str | None, published_at: str | None) -> tuple[str | None, str | None, str | None]:
    raw = clean(raw_time)
    if not raw:
        return None, None, None
    normalized = resolve_relative_time(raw, published_at) if published_at else raw
    date_match = DATE_RE.search(normalized)
    if date_match:
        period = f"{date_match['year']}-{int(date_match['month']):02d}-{int(date_match['day']):02d}"
        return period, period, "day"
    month_match = YEAR_MONTH_RE.search(normalized)
    if month_match:
        period = f"{month_match['year']}-{int(month_match['month']):02d}"
        return period, period, "month"
    quarter_match = QUARTER_RE.search(normalized)
    if quarter_match:
        year_match = YEAR_RE.search(normalized)
        period = f"{year_match['year']}-Q{quarter_match['quarter']}" if year_match else normalized
        return period, period, "quarter"
    year_match = YEAR_RE.search(normalized)
    if year_match:
        return year_match["year"], year_match["year"], "year"
    return normalized, normalized, "unknown"


def bool_value(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def optional_int(value) -> int | None:
    text = clean(value)
    if text is None:
        return None
    return int(float(text))


def observation_relation_type(index: int, change_type: str | None) -> str:
    """관측값별 relation_type을 보수적으로 부여한다(RELATION_TYPES 통제 어휘).

    규칙 변환기는 어느 값이 비교 기준인지 신뢰성 있게 판별할 수 없으므로 선두
    관측값만 primary로 두고 나머지는 미상(untyped)으로 둔다. 순위 주장은 나머지를
    동료(rank_peer)로 표시한다. 정확한 역할(comparison_base/component/total)은
    HCX 스키마화 단계가 최종 권위로 채운다.
    """
    if index == 0:
        return "primary"
    if change_type and "순위" in change_type:
        return "rank_peer"
    return "untyped"


def make_claim(row: pd.Series, row_number: int) -> Claim:
    values = split_list(row.get("value_list"))
    units = split_list(row.get("unit_list"))
    status = clean(row.get("list_alignment_status"))
    article_idx = int(row["article_idx"])
    claim_id = f"article_{article_idx}_row_{row_number:06d}"
    published_at = clean(row.get("작성일"))

    _, time_start, period_type = normalize_time(clean(row.get("time_ref")), published_at)
    change_type = clean(row.get("change_type"))
    is_index = bool_value(row.get("is_index"))

    def field(name: str, fallback: str | None = None) -> str | None:
        return clean(row.get(name)) or fallback

    source_raw = field("gold_source_org_raw", field("source_org_raw"))
    source_role = field("gold_source_role", "cited_source")
    attributions = []
    if source_raw:
        attributions.append(Attribution(
            org_raw=source_raw,
            org_id=field("gold_org_id"),
            org_name=field("gold_org_name"),
            role=source_role,
            evidence_quote=field("gold_source_evidence_quote"),
            status="explicit_same_sentence" if bool_value(row.get("source_mentioned")) else "ambiguous",
        ))

    auto_indicator = field("indicator_raw")
    auto_population = field("population_raw", field("population"))
    auto_dimensions = json_object(row.get("dimension_json"))
    context_resolution = json_object(row.get("context_resolution_json"))
    gold_indicator = field("gold_indicator_raw")
    gold_population = field("gold_population")
    claim = Claim(
        claim_id=claim_id,
        article_idx=article_idx,
        claim_text=str(row.get("claim_text", "")),
        source_row_number=row_number,
        sentence_index=optional_int(row.get("sentence_index")),
        sentence_char_start=optional_int(row.get("sentence_char_start")),
        sentence_char_end=optional_int(row.get("sentence_char_end")),
        article_title=clean(row.get("기사제목")),
        published_at=published_at,
        evidence_quote=str(row.get("claim_text", "")),
        context_resolution=context_resolution,
        retrieval_query_text=clean(row.get("retrieval_query_text")),
        indicator_raw=gold_indicator or auto_indicator,
        population_raw=gold_population or auto_population,
        auto_indicator_raw=auto_indicator,
        auto_population_raw=auto_population,
        auto_dimension_json=auto_dimensions,
        change_type=change_type,
        raw_value_list=values,
        raw_unit_list=units,
        observations=[],
        attributions=attributions,
        claim_class=field("gold_claim_class"),
        source_scope=field("gold_source_scope"),
        verifiability_prefilter=field("gold_verifiability_prefilter"),
        list_alignment_status=status,
        extraction_method="claim_extractor_listform",
        review_status=field("review_status", "pending") or "pending",
    )
    claim_indicator_raw = gold_indicator or auto_indicator
    for index, value_raw_item in enumerate(values):
        unit_raw_item = units[index] if index < len(units) else (units[0] if len(units) == 1 else None)
        value_num_item, unit_norm_item = normalize_value(value_raw_item, unit_raw_item)
        observation_period = clean(row.get("time_ref")) if index == 0 else None
        claim.observations.append(
            ClaimObservation(
                observation_id=f"{claim_id}_obs_{index + 1:03d}",
                claim_id=claim_id,
                indicator_raw=claim_indicator_raw if index == 0 else None,
                value_raw=value_raw_item,
                value_num=value_num_item,
                unit_raw=unit_raw_item,
                unit_norm=unit_norm_item,
                period_raw=observation_period,
                period_start=time_start if index == 0 else None,
                period_end=time_start if index == 0 else None,
                period_type=period_type if index == 0 else None,
                time_compare_raw=clean(row.get("time_compare")) if index == 0 else None,
                dimension_json=auto_dimensions,
                is_index=is_index,
                relation_type=observation_relation_type(index, change_type),
                comparison_group=claim_id,
                sequence=index,
                verify_scope=classify_observation_verify_scope(unit_norm_item),
            )
        )
    return claim


def make_gold_mapping(row: pd.Series, claim: Claim) -> ClaimTableMapping | None:
    tbl_id = clean(row.get("gold_tbl_id"))
    org_id = clean(row.get("gold_org_id"))
    if not tbl_id or not org_id:
        return None
    dimensions = {}
    raw_dimensions = clean(row.get("gold_dimension_json"))
    if raw_dimensions:
        try:
            dimensions = json.loads(raw_dimensions)
        except json.JSONDecodeError:
            dimensions = {"_raw": raw_dimensions}
    table_key = f"{org_id}:{tbl_id}"
    return ClaimTableMapping(
        mapping_id=f"gold_{claim.claim_id}",
        claim_id=claim.claim_id,
        table_key=table_key,
        retrieval_stage="gold_annotation",
        rank=1,
        matched_dimensions=dimensions,
        matched_item_id=clean(row.get("gold_item_id")),
        matched_period=clean(row.get("gold_period")),
        matched_unit=clean(row.get("gold_unit")),
        status="SELECTED",
        is_gold=True,
        selected=True,
        retrieval_version="gold-v1",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--mapping-output", type=Path, default=None)
    args = parser.parse_args()
    mapping_output = args.mapping_output or args.output.with_name(f"{args.output.stem}_mappings.jsonl")

    df = pd.read_csv(args.input, dtype=str, keep_default_na=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mappings = []
    errors = []
    with args.output.open("w", encoding="utf-8") as output:
        for row_number, (_, row) in enumerate(df.iterrows()):
            claim = make_claim(row, row_number)
            claim_errors = validate_claim(claim)
            if claim_errors:
                errors.append({"claim_id": claim.claim_id, "errors": claim_errors})
            output.write(json.dumps({"schema_version": "claim-v1", "claim": claim.__dict__}, ensure_ascii=False, default=lambda x: x.__dict__) + "\n")
            mapping = make_gold_mapping(row, claim)
            if mapping:
                mappings.append(mapping)

    with mapping_output.open("w", encoding="utf-8") as output:
        for mapping in mappings:
            output.write(json.dumps({"schema_version": "mapping-v1", "mapping": mapping.__dict__}, ensure_ascii=False) + "\n")

    print(f"claims={len(df)} -> {args.output}")
    print(f"gold_mappings={len(mappings)} -> {mapping_output}")
    print(f"validation_errors={len(errors)}")
    if errors:
        print(json.dumps(errors[:10], ensure_ascii=False))


if __name__ == "__main__":
    main()
