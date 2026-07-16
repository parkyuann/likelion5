"""실전2 검색용 Claim / KOSIS Catalog / Mapping 스키마 v1.

세 객체를 분리한다.

* Claim: 뉴스가 무엇을 주장했는가
* KOSISTable: 어떤 표와 메타데이터가 존재하는가
* ClaimTableMapping: claim과 표 후보가 어떻게 연결되는가

이 모듈은 외부 validation 패키지 없이 최소 구조 검증을 제공한다. CSV에서
이 구조로 변환할 때는 `make_claim_from_row()`를 사용하고, 실제 KOSIS 표
메타데이터는 `KOSISTable` 구조에 맞춰 적재한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CLAIM_CLASSES = {
    "집계통계", "개별사례", "전망예측", "목표계획", "법령제도",
    "해석수사", "사고대응임시통계", "여론조사", "통계조사안내", "정정보도",
}
SOURCE_SCOPES = {"KOSIS등재", "공식기관_비KOSIS", "민간기관", "해외기관", "불명"}
SOURCE_ROLES = {"data_producer", "announcer", "claimer", "comparison_source", "cited_source"}
ALIGNMENT_STATUSES = {"ALIGNED", "COUNT_MISMATCH", "LOW_CONFIDENCE", "NOT_LIST_FORM", "SINGLE_VALUE"}
MATCH_STATUSES = {"CANDIDATE", "SELECTED", "REJECTED", "AMBIGUOUS", "NO_KOSIS_MATCH"}


@dataclass
class Attribution:
    org_raw: str | None = None
    org_id: str | None = None
    org_name: str | None = None
    role: str = "cited_source"
    evidence_quote: str | None = None
    status: str = "ambiguous"


@dataclass
class Claim:
    """표 매핑에 필요한 원자적 claim 1개."""

    claim_id: str
    article_idx: int
    claim_text: str
    source_row_number: int | None = None
    article_title: str | None = None
    published_at: str | None = None
    evidence_quote: str | None = None

    indicator_raw: str | None = None
    indicator_norm: str | None = None
    population_raw: str | None = None
    population_norm: str | None = None
    value_raw: str | None = None
    value_num: float | None = None
    unit_raw: str | None = None
    unit_norm: str | None = None
    comparison_value_raw: str | None = None
    comparison_value_num: float | None = None
    comparison_unit_norm: str | None = None
    change_type: str | None = None
    direction: str | None = None
    time_ref_raw: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    period_type: str | None = None
    time_compare_raw: str | None = None
    time_compare_start: str | None = None
    time_compare_end: str | None = None
    is_index: bool = False
    index_base_period: str | None = None
    approximation_qualifier: str | None = None
    raw_value_list: list[str] = field(default_factory=list)
    raw_unit_list: list[str] = field(default_factory=list)

    attributions: list[Attribution] = field(default_factory=list)
    claim_class: str | None = None
    source_scope: str | None = None
    verifiability_prefilter: str | None = None
    list_alignment_status: str | None = None
    extraction_method: str | None = None
    review_status: str = "pending"


@dataclass
class KOSISPeriod:
    period_type: str
    start_period: str | None = None
    end_period: str | None = None


@dataclass
class KOSISDimension:
    dimension_id: str
    dimension_name: str
    values: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class KOSISTable:
    """벡터 DB에 적재할 표 metadata와 원문 문서."""

    table_key: str
    org_id: str
    org_name: str
    tbl_id: str
    tbl_name: str
    stat_id: str | None = None
    stat_name: str | None = None
    category_path: list[str] = field(default_factory=list)
    periods: list[KOSISPeriod] = field(default_factory=list)
    dimensions: list[KOSISDimension] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    source_name: str | None = None
    version_status: str | None = None
    document_text: str | None = None
    embedding_model: str | None = None
    embedding_version: str | None = None
    document_version: str = "table-doc-v1"


@dataclass
class ClaimTableMapping:
    """검색 단계별 claim-표 후보와 최종 선택을 저장한다."""

    mapping_id: str
    claim_id: str
    table_key: str
    retrieval_stage: str
    rank: int
    dense_score: float | None = None
    sparse_score: float | None = None
    reranker_score: float | None = None
    metadata_score: float | None = None
    matched_dimensions: dict[str, str] = field(default_factory=dict)
    matched_item_id: str | None = None
    matched_period: str | None = None
    matched_unit: str | None = None
    status: str = "CANDIDATE"
    filter_reason: str | None = None
    is_gold: bool = False
    selected: bool = False
    retrieval_version: str | None = None


def validate_claim(claim: Claim) -> list[str]:
    """Claim이 KOSIS 검색 입력으로 최소 요건을 갖췄는지 검사한다."""
    errors: list[str] = []
    if not claim.claim_id:
        errors.append("claim_id is required")
    if not isinstance(claim.article_idx, int):
        errors.append("article_idx must be int")
    if not claim.claim_text:
        errors.append("claim_text is required")
    if claim.claim_class and claim.claim_class not in CLAIM_CLASSES:
        errors.append(f"invalid claim_class: {claim.claim_class}")
    if claim.source_scope and claim.source_scope not in SOURCE_SCOPES:
        errors.append(f"invalid source_scope: {claim.source_scope}")
    if claim.list_alignment_status and claim.list_alignment_status not in ALIGNMENT_STATUSES:
        errors.append(f"invalid list_alignment_status: {claim.list_alignment_status}")
    for attribution in claim.attributions:
        if attribution.role not in SOURCE_ROLES:
            errors.append(f"invalid source role: {attribution.role}")
    if claim.value_num is not None and not claim.unit_norm:
        errors.append("unit_norm is required when value_num is present")
    if claim.time_start and claim.time_end and claim.time_start > claim.time_end:
        errors.append("time_start must not be after time_end")
    return errors


def validate_table(table: KOSISTable) -> list[str]:
    errors: list[str] = []
    for name in ("table_key", "org_id", "tbl_id", "tbl_name"):
        if not getattr(table, name):
            errors.append(f"{name} is required")
    if table.table_key != f"{table.org_id}:{table.tbl_id}":
        errors.append("table_key must be org_id:tbl_id")
    return errors


def validate_mapping(mapping: ClaimTableMapping) -> list[str]:
    errors: list[str] = []
    if mapping.rank < 1:
        errors.append("rank must be >= 1")
    if mapping.status not in MATCH_STATUSES:
        errors.append(f"invalid mapping status: {mapping.status}")
    if mapping.selected and mapping.status != "SELECTED":
        errors.append("selected mapping must have status=SELECTED")
    return errors
