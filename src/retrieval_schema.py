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
# 결정론 검증 결과(list_alignment_status): change_type별 계산 후 원본 목록 정합성 판정.
ALIGNMENT_STATUSES = {"ALIGNED", "COUNT_MISMATCH", "LOW_CONFIDENCE", "NOT_LIST_FORM", "SINGLE_VALUE"}
MATCH_STATUSES = {"CANDIDATE", "SELECTED", "REJECTED", "AMBIGUOUS", "NO_KOSIS_MATCH"}

# ClaimObservation.relation_type — 관측값 1개가 주장 안에서 갖는 역할.
# 비교/구성 관계는 flat pair가 아니라 관측값 여러 개(같은 comparison_group)를
# sequence로 연결해 표현한다. HCX 스키마화가 최종 권위이며 규칙 변환기는 보수적
# 기본값(선두=primary, 나머지=untyped)만 부여한다.
#   primary          기준이 되는 주값 (예: 올 1분기 순이익 92억)
#   comparison_base  주값과 비교되는 기준 관측값 (예: 작년 1분기 46억)
#   component        전체를 구성하는 부분값
#   total            부분값들의 합계/전체값
#   rank_peer        순위 목록에서 비교되는 동료 항목
#   untyped          역할 미상
RELATION_TYPES = {"primary", "comparison_base", "component", "total", "rank_peer", "untyped"}

# Claim.overall_status — 최종 verdict. UNVERIFIABLE은 abstention(확인 불가) 명시용.
OVERALL_STATUSES = {"VERIFIED", "REFUTED", "UNVERIFIABLE", "PARTIAL"}
# 문장 밖 문맥으로 지시 대상(예: "해당 보험")을 보강한 결과. 모호한 상태는
# 검색 recall을 위해 원문 claim으로만 후보를 찾을 수는 있어도 셀 정렬을 확정하면 안 된다.
CONTEXT_RESOLUTION_STATUSES = {
    "NOT_APPLICABLE", "EXPLICIT", "RESOLVED", "REFERENT_CANDIDATE", "REFERENT_AMBIGUOUS", "CONTEXT_MISSING",
}

# 문맥 referent 판정과 KOSIS 표·셀 매핑 가능성은 다른 축이다. 예를 들어
# CONTEXT_MISSING이라도 지표·기간·대상이 자족하면 claim-only 검색은 가능할 수 있다.
MAPPING_ELIGIBILITIES = {
    "OUT_OF_SCOPE", "CONTEXT_EXPANDED", "CLAIM_ONLY_SAFE", "CONTEXT_REQUIRED_UNRESOLVED",
}

# ClaimTableMapping.align_status — v2 호환용 단일 상태 축. 새 R4 계약에서는
# resolution_status(API 전)와 cell_status(API 후)가 권위 필드다.
ALIGN_STATUSES = {"ALIGNED", "DIM_MISSING", "PERIOD_MISMATCH", "ITEM_AMBIGUOUS", "NO_CELL"}
RESOLUTION_STATUSES = {
    "QUERY_READY", "ITEM_AMBIGUOUS", "AXIS_AMBIGUOUS", "DIM_MISSING",
    "PERIOD_MISMATCH", "UNIT_MISMATCH", "PROFILE_INCOMPLETE",
    "DERIVED_READY", "DERIVED_RANGE",
}
CELL_STATUSES = {
    "NOT_QUERIED", "CELL_RESOLVED", "NO_CELL", "MULTIPLE_CELLS", "API_ERROR",
}
MEANING_ROLE_HINTS = {
    "indicator", "item", "population", "dimension",
    "population_or_dimension", "unknown",
}
AXIS_KINDS = {"ITEM", "DIMENSION"}

# dimension_json 통제 어휘 — 정렬 시 키 표류(지역/시도/region 혼용)를 막기 위한
# 최소 허용 키 집합. 새 차원이 필요하면 여기에 먼저 추가한다.
DIMENSION_KEYS = {"지역", "성별", "연령", "산업", "항목"}

# noise_reason — is_claim=False일 때 "왜 주장이 아닌가"를 남기는 진단 축(점수 미산정).
# claim_class(10종)와 별개 축이며, 노이즈를 claim_class 11번째로 뭉개지 않고 분리한다.
NOISE_REASONS = {"광고", "의견", "질문", "불완전문", "인용맥락", "UI잡음", "기타"}

# verifiability_prefilter — 검색 앞단 디부스트 게이트(3값). 하드드롭 대신 불확실 케이스는
# 강등해 검색 기회를 주되 우선순위만 낮춘다(분류기 오류로 인한 리콜 손실 방지).
VERIFIABILITY_PREFILTERS = {"검증시도", "강등", "제외"}

# verify_scope — observation 단위 검증대상 판정(G). 실전1이 부여, 정렬기(T3-2)가 소비한다.
# aggregate/unknown만 셀 정렬 대상이며 time_ref/noise/individual은 검증에서 뺀다.
# G-a는 결정론으로 time_ref만 표시하고, aggregate/individual 판정은 G-b(LLM)가 채운다.
OBSERVATION_VERIFY_SCOPES = {"aggregate", "individual", "noise", "time_ref", "unknown"}
_TIME_UNIT_NORMS = {"년", "개월", "월", "분기", "일", "주", "시간", "분", "초"}


def classify_observation_verify_scope(unit_norm: str | None) -> str:
    """observation의 검증대상성을 규칙 우선으로 판정한다(G-a: 결정론 부분).

    시간·기간 단위 관측값은 통계값이 아니라 기간·연차·지속시간·오탈("3분의1"→분,
    "90 초반"→초) 토큰이므로 time_ref로 표시해 검증 대상에서 뺀다. 근로시간·근속연수
    같은 정상 시간값은 소수 false-exclude이며 G-b(LLM)가 aggregate로 재승격한다.
    나머지 단위는 unknown으로 두고 G-b가 aggregate/individual을 판정한다.
    """
    if (unit_norm or "").strip() in _TIME_UNIT_NORMS:
        return "time_ref"
    return "unknown"


@dataclass
class Attribution:
    org_raw: str | None = None
    org_id: str | None = None
    org_name: str | None = None
    role: str = "cited_source"
    evidence_quote: str | None = None
    status: str = "ambiguous"


@dataclass
class MeaningAtom:
    """원문 근거가 있는 claim 의미 단위이며 KOSIS axis 역할은 확정하지 않는다."""

    atom_id: str
    raw: str
    normalized: str
    role_hint: str = "unknown"
    source_span: str | None = None
    observation_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimObservation:
    """주장 안의 측정값 1개. 값·단위·시점·차원·지수·근사 등 모든 물리적 속성의
    canonical 저장소이며, 관측값 사이의 관계(비교·구성·순위)도 여기서 표현한다.

    한 주장에 지표가 여러 개 섞이면(예: 순이익 92억 + 매출 1704억) 관측값별
    indicator_norm으로 어느 지표인지 구분한다 — 다지표 셀 정렬의 전제.
    """

    observation_id: str
    claim_id: str
    # 관측값별 지표(다지표 주장에서 어느 지표/항목인지 구분)
    indicator_raw: str | None = None
    indicator_norm: str | None = None
    value_raw: str | None = None
    value_num: float | None = None
    unit_raw: str | None = None
    unit_norm: str | None = None
    period_raw: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    period_type: str | None = None
    time_compare_raw: str | None = None
    dimension_json: dict[str, Any] = field(default_factory=dict)
    # R4 후보별 axis resolver 입력. role_hint는 의미 힌트일 뿐 ITEM/obj 역할이 아니다.
    meaning_atoms: list[MeaningAtom] = field(default_factory=list)
    # 지수·근사 속성(Claim에서 이관) — 값과 함께 관측값에 귀속된다.
    is_index: bool = False
    index_base_period: str | None = None
    approximation_qualifier: str | None = None  # 원문 근사·반올림 표현("약 3%") 표시 = rounding_hint
    relation_type: str = "untyped"
    comparison_group: str | None = None
    sequence: int = 0
    verify_scope: str = "unknown"  # 검증대상 판정(G): aggregate/individual/noise/time_ref/unknown


@dataclass
class Claim:
    """주장 1건의 정체성·맥락·검색키·분류·검증연산 종류 (주장당 1개).

    값·단위·시점·지수·근사 등 측정값의 물리적 속성은 ClaimObservation이
    canonical 저장소다(옵션 A: Claim의 flat 값/시점 필드는 제거). 여러 지표·비교값이
    섞인 주장은 observations 여러 개로 표현한다.
    """

    claim_id: str
    article_idx: int
    claim_text: str
    source_row_number: int | None = None
    # Position in cleaned article text; char_end is exclusive.
    sentence_index: int | None = None
    sentence_char_start: int | None = None
    sentence_char_end: int | None = None
    article_title: str | None = None
    published_at: str | None = None
    evidence_quote: str | None = None
    # 문맥 보강은 원문 claim을 덮어쓰지 않는다. resolver가 상태·근거 sentence/span·
    # retrieval query 확장 여부를 이 audit object에 보존한다.
    context_resolution: dict[str, Any] = field(default_factory=dict)
    retrieval_query_text: str | None = None
    mapping_eligibility: str | None = None

    # 검색 키 — 주장 대표 지표/모집단(관측값별 세부 지표는 observation.indicator_norm)
    indicator_raw: str | None = None
    indicator_norm: str | None = None
    population_raw: str | None = None
    population_norm: str | None = None
    # Rule/HCX extraction is kept separately from human gold annotations.
    auto_indicator_raw: str | None = None
    auto_population_raw: str | None = None
    auto_dimension_json: dict[str, Any] = field(default_factory=dict)
    # 검증 연산 종류(증감률/비교/순위 등)와 방향 — 값 자체는 observations에 있다.
    change_type: str | None = None
    direction: str | None = None
    # 규칙 추출 원본 목록 보존(정규화 전 값·단위)
    raw_value_list: list[str] = field(default_factory=list)
    raw_unit_list: list[str] = field(default_factory=list)
    observations: list[ClaimObservation] = field(default_factory=list)

    attributions: list[Attribution] = field(default_factory=list)
    # 판정 3축(조건부 계층): is_claim(노이즈 게이트) → claim_class(유형, is_claim=True일 때만)
    #                        / noise_reason(진단, is_claim=False일 때만)
    is_claim: bool | None = None
    claim_class: str | None = None
    noise_reason: str | None = None
    source_scope: str | None = None
    verifiability_prefilter: str | None = None
    list_alignment_status: str | None = None
    overall_status: str | None = None  # 최종 verdict (VERIFIED/REFUTED/UNVERIFIABLE/PARTIAL)
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
class CandidateAxis:
    """후보 표 하나의 ITEM 또는 dimension axis를 순서와 provenance까지 보존한다."""

    axis_kind: str
    axis_id: str
    axis_name: str
    position: int | None = None
    values: list[dict[str, Any]] = field(default_factory=list)
    metadata_complete: bool = True


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
    # v3 catalog keeps dense and sparse retrieval documents separate.
    doc_meta_text: str | None = None
    doc_item_index: str | None = None
    catalog_version: str | None = None
    value_parse_status: str | None = None
    # v4 catalog provenance: category path availability and per-endpoint metadata state.
    category_path_status: str | None = None
    api_status: dict[str, str] = field(default_factory=dict)
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
    # R4 권위 상태: API 전 의미/schema resolution과 API 후 cell 결과를 분리한다.
    resolution_status: str | None = None
    cell_status: str = "NOT_QUERIED"
    match_evidence: list[dict[str, Any]] = field(default_factory=list)
    competing_matches: list[dict[str, Any]] = field(default_factory=list)
    defaulted_axes: list[dict[str, Any]] = field(default_factory=list)
    query_plan: dict[str, Any] = field(default_factory=dict)
    # v2 소비자용 호환 필드. 새 산출물의 권위 필드로 사용하지 않는다.
    align_status: str | None = None
    status: str = "CANDIDATE"
    filter_reason: str | None = None
    is_gold: bool = False
    selected: bool = False
    retrieval_version: str | None = None


def compute_verifiability_prefilter(
    is_claim: bool | None, claim_class: str | None, source_scope: str | None
) -> tuple[str, str]:
    """(is_claim, claim_class, source_scope)에서 3값 디부스트 게이트를 결정론으로 계산한다.

    반환: (prefilter, reason). 하드게이트(집계통계+KOSIS등재만 통과)와 달리, 출처가
    미확정이거나(불명/공식기관_비KOSIS) 미분류인 집계통계는 '제외'가 아니라 '강등'으로
    두어 검색에서 회수할 수 있게 한다. 확실히 범위 밖(비주장/민간·해외 출처/비집계
    claim_class)만 '제외'한다.
    """
    if is_claim is False:
        return "제외", "비주장(is_claim=False)"
    if claim_class == "집계통계":
        if source_scope == "KOSIS등재":
            return "검증시도", "집계통계 + KOSIS등재"
        if source_scope in ("공식기관_비KOSIS", "불명", None, ""):
            return "강등", f"집계통계이나 출처 미확정({source_scope or '불명'})"
        return "제외", f"집계통계이나 KOSIS 범위 밖 출처({source_scope})"
    if not claim_class:
        return "강등", "주장이나 claim_class 미분류"
    return "제외", f"KOSIS 대조 대상 아님(claim_class={claim_class})"


def validate_claim(claim: Claim) -> list[str]:
    """Claim이 KOSIS 검색 입력으로 최소 요건을 갖췄는지 검사한다."""
    errors: list[str] = []
    if not claim.claim_id:
        errors.append("claim_id is required")
    if not isinstance(claim.article_idx, int):
        errors.append("article_idx must be int")
    if not claim.claim_text:
        errors.append("claim_text is required")
    if claim.sentence_index is not None and (
        not isinstance(claim.sentence_index, int) or claim.sentence_index < 0
    ):
        errors.append("sentence_index must be a non-negative int")
    for name in ("sentence_char_start", "sentence_char_end"):
        value = getattr(claim, name)
        if value is not None and (not isinstance(value, int) or value < 0):
            errors.append(f"{name} must be a non-negative int")
    if (
        claim.sentence_char_start is not None
        and claim.sentence_char_end is not None
        and claim.sentence_char_end < claim.sentence_char_start
    ):
        errors.append("sentence_char_end must be >= sentence_char_start")
    if claim.claim_class and claim.claim_class not in CLAIM_CLASSES:
        errors.append(f"invalid claim_class: {claim.claim_class}")
    # 조건부 계층 정합: is_claim=False ⇒ claim_class=null, is_claim=True ⇒ noise_reason=null
    if claim.is_claim is False and claim.claim_class:
        errors.append("claim_class must be null when is_claim is False")
    if claim.is_claim is True and claim.noise_reason:
        errors.append("noise_reason must be null when is_claim is True")
    if claim.noise_reason and claim.noise_reason not in NOISE_REASONS:
        errors.append(f"invalid noise_reason: {claim.noise_reason}")
    if claim.verifiability_prefilter and claim.verifiability_prefilter not in VERIFIABILITY_PREFILTERS:
        errors.append(f"invalid verifiability_prefilter: {claim.verifiability_prefilter}")
    if claim.source_scope and claim.source_scope not in SOURCE_SCOPES:
        errors.append(f"invalid source_scope: {claim.source_scope}")
    if claim.list_alignment_status and claim.list_alignment_status not in ALIGNMENT_STATUSES:
        errors.append(f"invalid list_alignment_status: {claim.list_alignment_status}")
    if claim.overall_status and claim.overall_status not in OVERALL_STATUSES:
        errors.append(f"invalid overall_status: {claim.overall_status}")
    if claim.context_resolution:
        context_status = claim.context_resolution.get("status")
        if context_status not in CONTEXT_RESOLUTION_STATUSES:
            errors.append(f"invalid context resolution status: {context_status}")
    if claim.mapping_eligibility and claim.mapping_eligibility not in MAPPING_ELIGIBILITIES:
        errors.append(f"invalid mapping_eligibility: {claim.mapping_eligibility}")
    for attribution in claim.attributions:
        if attribution.role not in SOURCE_ROLES:
            errors.append(f"invalid source role: {attribution.role}")
    observation_ids = set()
    for observation in claim.observations:
        if observation.claim_id != claim.claim_id:
            errors.append("observation.claim_id must match claim.claim_id")
        if observation.observation_id in observation_ids:
            errors.append("observation_id must be unique within a claim")
        observation_ids.add(observation.observation_id)
        if observation.value_num is not None and not observation.unit_norm:
            errors.append("observation.unit_norm is required when value_num is present")
        if observation.relation_type not in RELATION_TYPES:
            errors.append(f"invalid relation_type: {observation.relation_type}")
        if observation.verify_scope not in OBSERVATION_VERIFY_SCOPES:
            errors.append(f"invalid verify_scope: {observation.verify_scope}")
        if observation.period_start and observation.period_end and observation.period_start > observation.period_end:
            errors.append("observation.period_start must not be after period_end")
        for key in observation.dimension_json:
            if key not in DIMENSION_KEYS:
                errors.append(f"invalid dimension key: {key}")
        atom_ids = set()
        for atom in observation.meaning_atoms:
            if not atom.atom_id:
                errors.append("meaning atom_id is required")
            elif atom.atom_id in atom_ids:
                errors.append("meaning atom_id must be unique within an observation")
            atom_ids.add(atom.atom_id)
            if not atom.raw or not atom.normalized:
                errors.append("meaning atom raw and normalized are required")
            if atom.role_hint not in MEANING_ROLE_HINTS:
                errors.append(f"invalid meaning role_hint: {atom.role_hint}")
            if atom.observation_id and atom.observation_id != observation.observation_id:
                errors.append("meaning atom observation_id must match observation")
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
    if mapping.align_status and mapping.align_status not in ALIGN_STATUSES:
        errors.append(f"invalid align_status: {mapping.align_status}")
    if mapping.resolution_status and mapping.resolution_status not in RESOLUTION_STATUSES:
        errors.append(f"invalid resolution_status: {mapping.resolution_status}")
    if mapping.cell_status not in CELL_STATUSES:
        errors.append(f"invalid cell_status: {mapping.cell_status}")
    if mapping.resolution_status == "QUERY_READY":
        if not mapping.matched_item_id:
            errors.append("QUERY_READY requires matched_item_id")
        if not mapping.query_plan:
            errors.append("QUERY_READY requires query_plan")
    elif mapping.resolution_status and mapping.cell_status != "NOT_QUERIED":
        errors.append("blocked resolution must have cell_status=NOT_QUERIED")
    if mapping.cell_status != "NOT_QUERIED":
        if mapping.resolution_status != "QUERY_READY":
            errors.append("queried cell status requires resolution_status=QUERY_READY")
        if not mapping.query_plan:
            errors.append("queried cell status requires query_plan")
    if mapping.selected and mapping.status != "SELECTED":
        errors.append("selected mapping must have status=SELECTED")
    return errors
