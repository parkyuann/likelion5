"""retrieval_schema v2 필드/enum 검증 테스트.

실행: venv/Scripts/python.exe -m pytest tests/test_retrieval_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retrieval_schema import (  # noqa: E402
    ALIGN_STATUSES,
    DIMENSION_KEYS,
    NOISE_REASONS,
    OVERALL_STATUSES,
    RELATION_TYPES,
    VERIFIABILITY_PREFILTERS,
    Claim,
    ClaimObservation,
    ClaimTableMapping,
    CONTEXT_RESOLUTION_STATUSES,
    MAPPING_ELIGIBILITIES,
    KOSISTable,
    compute_verifiability_prefilter,
    validate_claim,
    validate_mapping,
    validate_table,
)


def make_observation(**overrides) -> ClaimObservation:
    base = dict(
        observation_id="c1_obs_001",
        claim_id="c1",
        value_num=9_200_000_000.0,
        unit_norm="위안",
        relation_type="primary",
        sequence=0,
    )
    base.update(overrides)
    return ClaimObservation(**base)


def make_claim(**overrides) -> Claim:
    base = dict(claim_id="c1", article_idx=1522, claim_text="순이익 92억위안")
    base.update(overrides)
    return Claim(**base)


# --- 옵션 A: Claim에서 flat 값/시점 필드가 제거되었는지 ---------------------

def test_claim_has_no_flat_value_fields():
    claim = make_claim()
    for removed in (
        "value_num", "value_raw", "unit_norm", "unit_raw",
        "comparison_value_num", "comparison_unit_norm",
        "time_start", "time_end", "period_type", "time_compare_start",
        "is_index", "index_base_period", "approximation_qualifier",
    ):
        assert not hasattr(claim, removed), f"flat 필드 {removed} 는 제거되어야 한다"


def test_observation_owns_physical_attributes():
    obs = make_observation(is_index=True, index_base_period="2020", approximation_qualifier="약")
    for owned in ("indicator_norm", "indicator_raw", "is_index", "index_base_period", "approximation_qualifier"):
        assert hasattr(obs, owned)
    assert obs.is_index is True
    assert obs.index_base_period == "2020"


# --- 다지표 관측값 구분(change 1) -------------------------------------------

def test_multi_indicator_observations_are_distinguishable():
    claim = make_claim(observations=[
        make_observation(observation_id="c1_obs_001", indicator_norm="순이익", value_num=9.2e9, unit_norm="위안"),
        make_observation(observation_id="c1_obs_002", indicator_norm="매출", value_num=1.704e11,
                         unit_norm="위안", sequence=1, relation_type="primary"),
    ])
    assert validate_claim(claim) == []
    indicators = {o.indicator_norm for o in claim.observations}
    assert indicators == {"순이익", "매출"}


# --- 비교를 관측값 2개로 표현(change 3) -------------------------------------

def test_comparison_expressed_as_two_observations():
    claim = make_claim(observations=[
        make_observation(observation_id="c1_obs_001", value_num=9.2e9, unit_norm="위안",
                         relation_type="primary", comparison_group="c1", sequence=0),
        make_observation(observation_id="c1_obs_002", value_num=4.6e9, unit_norm="위안",
                         relation_type="comparison_base", comparison_group="c1", sequence=1),
    ])
    assert validate_claim(claim) == []
    base = [o for o in claim.observations if o.relation_type == "comparison_base"]
    assert len(base) == 1 and base[0].comparison_group == "c1"


# --- relation_type enum(change 4) -------------------------------------------

def test_relation_type_enum_values():
    assert RELATION_TYPES == {"primary", "comparison_base", "component", "total", "rank_peer", "untyped"}


def test_invalid_relation_type_rejected():
    claim = make_claim(observations=[make_observation(relation_type="time_series")])
    assert any("invalid relation_type" in e for e in validate_claim(claim))


# --- dimension_json 통제 어휘(change 5) -------------------------------------

def test_dimension_key_controlled_vocabulary():
    assert "지역" in DIMENSION_KEYS
    ok = make_claim(observations=[make_observation(dimension_json={"지역": "서울"})])
    assert validate_claim(ok) == []
    drift = make_claim(observations=[make_observation(dimension_json={"region": "Seoul"})])
    assert any("invalid dimension key" in e for e in validate_claim(drift))


# --- v2 상태 축 ------------------------------------------------------------

def test_overall_status_enum():
    assert OVERALL_STATUSES == {"VERIFIED", "REFUTED", "UNVERIFIABLE", "PARTIAL"}
    ok = make_claim(overall_status="UNVERIFIABLE", observations=[make_observation()])
    assert validate_claim(ok) == []
    bad = make_claim(overall_status="MAYBE", observations=[make_observation()])
    assert any("invalid overall_status" in e for e in validate_claim(bad))


def test_align_status_enum_on_mapping():
    assert ALIGN_STATUSES == {"ALIGNED", "DIM_MISSING", "PERIOD_MISMATCH", "ITEM_AMBIGUOUS", "NO_CELL"}
    mapping = ClaimTableMapping(
        mapping_id="m1", claim_id="c1", table_key="101:DT_1",
        retrieval_stage="align", rank=1, align_status="DIM_MISSING",
    )
    assert validate_mapping(mapping) == []
    mapping.align_status = "WHATEVER"
    assert any("invalid align_status" in e for e in validate_mapping(mapping))


# --- is_claim / claim_class / noise_reason 조건부 계층 ----------------------

def test_noise_reasons_vocab():
    assert NOISE_REASONS == {"광고", "의견", "질문", "불완전문", "인용맥락", "UI잡음", "기타"}


def test_is_claim_false_requires_null_claim_class():
    bad = make_claim(is_claim=False, claim_class="집계통계")
    assert any("claim_class must be null when is_claim is False" in e for e in validate_claim(bad))
    ok = make_claim(is_claim=False, claim_class=None, noise_reason="광고")
    assert validate_claim(ok) == []


def test_is_claim_true_requires_null_noise_reason():
    bad = make_claim(is_claim=True, claim_class="집계통계", noise_reason="광고",
                     observations=[make_observation()])
    assert any("noise_reason must be null when is_claim is True" in e for e in validate_claim(bad))


def test_invalid_noise_reason_rejected():
    bad = make_claim(is_claim=False, noise_reason="스팸")
    assert any("invalid noise_reason" in e for e in validate_claim(bad))


def test_legacy_claim_without_is_claim_axis_unaffected():
    # is_claim=None(기존 19k 데이터)은 정합 규칙을 건드리지 않는다.
    legacy = make_claim(claim_class="집계통계", observations=[make_observation()])
    assert validate_claim(legacy) == []


# --- verifiability_prefilter 디부스트 게이트(3값) --------------------------

def test_prefilter_verify_attempt():
    assert compute_verifiability_prefilter(True, "집계통계", "KOSIS등재")[0] == "검증시도"


def test_prefilter_deboost_on_unresolved_source():
    # 집계통계이나 출처 미확정 → 하드드롭 대신 강등(리콜 회수)
    assert compute_verifiability_prefilter(True, "집계통계", "불명")[0] == "강등"
    assert compute_verifiability_prefilter(True, "집계통계", "공식기관_비KOSIS")[0] == "강등"
    assert compute_verifiability_prefilter(True, "", "KOSIS등재")[0] == "강등"  # 미분류


def test_prefilter_exclude_out_of_scope():
    assert compute_verifiability_prefilter(False, None, None)[0] == "제외"  # 비주장
    assert compute_verifiability_prefilter(True, "집계통계", "민간기관")[0] == "제외"
    assert compute_verifiability_prefilter(True, "집계통계", "해외기관")[0] == "제외"
    assert compute_verifiability_prefilter(True, "개별사례", "KOSIS등재")[0] == "제외"


def test_prefilter_enum_validated():
    assert VERIFIABILITY_PREFILTERS == {"검증시도", "강등", "제외"}
    bad = make_claim(verifiability_prefilter="판단불가")
    assert any("invalid verifiability_prefilter" in e for e in validate_claim(bad))


# --- verify_scope (G-a: observation 단위 결정론 플래깅) --------------------

def test_verify_scope_time_ref_for_time_units():
    from retrieval_schema import classify_observation_verify_scope as f
    for unit in ("년", "개월", "분기", "시간", "분", "초"):
        assert f(unit) == "time_ref"


def test_verify_scope_unknown_for_stat_units():
    from retrieval_schema import classify_observation_verify_scope as f
    for unit in ("%", "원", "명", "건", None, ""):
        assert f(unit) == "unknown"


def test_invalid_verify_scope_rejected():
    claim = make_claim(observations=[make_observation(verify_scope="bogus")])
    assert any("invalid verify_scope" in e for e in validate_claim(claim))


def test_default_verify_scope_is_unknown():
    assert make_observation().verify_scope == "unknown"


def test_context_resolution_status_is_audited_on_claim():
    assert "REFERENT_AMBIGUOUS" in CONTEXT_RESOLUTION_STATUSES
    assert "REFERENT_CANDIDATE" in CONTEXT_RESOLUTION_STATUSES
    resolved = make_claim(context_resolution={"status": "RESOLVED"})
    assert validate_claim(resolved) == []
    invalid = make_claim(context_resolution={"status": "GUESS"})
    assert any("invalid context resolution status" in error for error in validate_claim(invalid))


def test_mapping_eligibility_is_a_separate_validated_axis():
    assert MAPPING_ELIGIBILITIES == {
        "OUT_OF_SCOPE", "CONTEXT_EXPANDED", "CLAIM_ONLY_SAFE", "CONTEXT_REQUIRED_UNRESOLVED",
    }
    assert validate_claim(make_claim(mapping_eligibility="CLAIM_ONLY_SAFE")) == []
    assert any("invalid mapping_eligibility" in error for error in validate_claim(make_claim(mapping_eligibility="MAYBE")))


# --- 기존 게이트 유지 ------------------------------------------------------

def test_observation_unit_required_when_value_present():
    claim = make_claim(observations=[make_observation(value_num=100.0, unit_norm=None)])
    assert any("unit_norm is required" in e for e in validate_claim(claim))


def test_observation_period_ordering():
    claim = make_claim(observations=[make_observation(period_start="2025-04", period_end="2025-01")])
    assert any("period_start must not be after period_end" in e for e in validate_claim(claim))


def test_valid_claim_passes():
    claim = make_claim(claim_class="집계통계", source_scope="KOSIS등재",
                       observations=[make_observation()])
    assert validate_claim(claim) == []


def test_table_key_gate_unchanged():
    table = KOSISTable(table_key="101:DT_1", org_id="101", org_name="통계청",
                       tbl_id="DT_1", tbl_name="t")
    assert validate_table(table) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
