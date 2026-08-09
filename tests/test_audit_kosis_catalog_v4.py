"""audit_kosis_catalog_v4의 측정 정확성을 손으로 만든 소형 fixture로 검증한다.

감사 도구가 틀리면 보고서 전체가 무효가 되므로, 각 측정 항목마다 결함을 정확히
1건씩 심고 카운트가 그대로 나오는지 확인한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "develop"))

from audit_kosis_catalog_v4 import (  # noqa: E402
    audit,
    classify_period,
    compare_value_counts,
    dimension_value_count,
    is_missing,
    summarize_distribution,
    write_outputs,
)

CURRENT_YEAR = 2026


def record(**overrides):
    """모든 필수 필드가 채워진 정상 레코드를 만들고 필요한 부분만 덮어쓴다."""
    base = {
        "table_key": "101:DT_1B040A3",
        "org_id": "101",
        "tbl_id": "DT_1B040A3",
        "tbl_name": "행정구역별 인구",
        "catalog_version": "kosis-catalog-v2",
        "doc_meta_text": "인구 | 행정구역별",
        "doc_item_index": "총인구 지역별",
        "items": [{"itm_id": "T1", "itm_nm": "총인구", "unit_nm": "명"}],
        "dimensions": [{"obj_id": "A", "obj_nm": "지역별", "values": [{"value_id": "11", "value_name": "서울"}]}],
        "units": ["명"],
        "period_types": ["년"],
        "latest_period": "2025",
        "meta_status": "enriched",
        "category_paths": [["인구"]],
        "source": "인구총조사",
        "stat_id": "1B040A3",
        "org_name": "통계청",
    }
    base.update(overrides)
    return base


def write_catalog(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8",
    )
    return path


def run(tmp_path: Path, records: list[dict], raw_meta: list[dict] | None = None) -> dict:
    catalog = write_catalog(tmp_path / "catalog.jsonl", records)
    meta_path = None
    if raw_meta is not None:
        meta_path = tmp_path / "meta.jsonl"
        write_catalog(meta_path, raw_meta)
    return audit(catalog, meta_path, CURRENT_YEAR)


def test_is_missing_treats_empty_containers_as_missing():
    # items: [] 는 '항목 없는 표'가 아니라 '항목을 못 가져온 표'다.
    assert is_missing(None) and is_missing("") and is_missing([]) and is_missing({})
    assert not is_missing(0) and not is_missing(["x"]) and not is_missing("0")


def test_clean_catalog_reports_no_defects(tmp_path):
    result = run(tmp_path, [record(), record(table_key="101:DT_B", tbl_id="DT_B")])
    totals = result["record_totals"]
    assert totals["total_records"] == 2
    assert totals["unique_table_keys"] == 2
    assert totals["duplicate_table_keys"] == 0
    assert totals["invalid_table_keys"] == 0
    assert totals["parse_failures"] == 0
    assert result["field_completeness"]["cell_query_ready"] == 2


def test_duplicate_table_key_is_counted_once_with_line_numbers(tmp_path):
    result = run(tmp_path, [record(), record(), record(table_key="101:DT_B", tbl_id="DT_B")])
    assert result["record_totals"]["total_records"] == 3
    assert result["record_totals"]["unique_table_keys"] == 2
    assert result["record_totals"]["duplicate_table_keys"] == 1
    assert result["record_totals"]["duplicate_extra_rows"] == 1
    duplicate = result["rows"]["duplicate_table_keys"][0]
    assert duplicate["occurrences"] == 2
    assert duplicate["line_numbers"] == [1, 2]


def test_table_key_mismatch_and_empty_ids_are_flagged(tmp_path):
    result = run(tmp_path, [
        record(),
        record(table_key="999:WRONG"),          # org:tbl 조합과 불일치
        record(table_key="101:", tbl_id=""),    # tbl_id 자체가 빔
    ])
    reasons = sorted(row["reason"] for row in result["rows"]["invalid_table_keys"])
    assert reasons == ["empty_org_or_tbl_id", "table_key_mismatch"]
    assert result["rows"]["invalid_table_keys"][0]["expected_table_key"] == "101:DT_1B040A3"


def test_missing_required_fields_are_listed_per_row(tmp_path):
    result = run(tmp_path, [record(), record(table_key="101:DT_B", tbl_id="DT_B", items=[], doc_meta_text="")])
    completeness = result["field_completeness"]
    assert completeness["required_fields"]["items"]["missing"] == 1
    assert completeness["required_fields"]["items"]["missing_rate"] == 0.5
    assert completeness["required_fields"]["doc_meta_text"]["missing"] == 1
    assert completeness["required_fields"]["dimensions"]["missing"] == 0
    assert completeness["rows_with_any_required_missing"] == 1
    row = result["rows"]["missing_required_fields"][0]
    assert sorted(row["missing_fields"]) == ["doc_meta_text", "items"]


def test_observed_field_missing_does_not_count_as_required(tmp_path):
    result = run(tmp_path, [record(units=[], source="")])
    completeness = result["field_completeness"]
    assert completeness["observed_fields"]["units"]["missing"] == 1
    assert completeness["observed_fields"]["source"]["missing"] == 1
    assert completeness["rows_with_any_required_missing"] == 0


def test_dimension_value_count_reads_both_schema_shapes():
    structured = {"obj_id": "A", "obj_nm": "지역별", "values": [{"value_id": "11"}, {"value_id": "26"}]}
    assert dimension_value_count(structured) == (2, True)
    # v2/v4 세대는 개수만 보존한다. 개수가 있어도 value_id가 없으면 structured가 아니다.
    assert dimension_value_count({"obj_id": "A", "obj_nm": "지역별", "value_count": 17}) == (17, False)
    assert dimension_value_count({"obj_id": "A", "obj_nm": "지역별"}) == (0, False)
    assert dimension_value_count({"obj_id": "A", "value_count": -1}) == (0, False)


def test_value_count_only_dimensions_are_not_cell_query_ready(tmp_path):
    """개수만 아는 차원은 '서울'을 어느 value_id로 조회할지 결정할 수 없다."""
    count_only = [{"obj_id": "A", "obj_nm": "지역별", "value_count": 17}]
    result = run(tmp_path, [record(), record(table_key="101:DT_B", tbl_id="DT_B", dimensions=count_only)])
    completeness = result["field_completeness"]
    representation = completeness["dimension_value_representation"]
    assert representation["structured_values_array"] == 1
    assert representation["value_count_only"] == 1
    assert representation["tables_with_all_dimensions_structured"] == 1
    assert completeness["cell_query_ready"] == 1
    # 값이 17개 있다는 사실 자체는 분포에 반영되고, 결측으로 세지 않는다.
    assert completeness["dimension_value_count_distribution"]["max"] == 17
    assert completeness["tables_with_zero_value_dimension"] == 0


def test_zero_value_dimension_blocks_cell_query_ready(tmp_path):
    # 차원 이름만 있고 값이 없으면 '서울'을 배치할 축이 없어 셀 조회가 불가능하다.
    empty_dimension = [{"obj_id": "A", "obj_nm": "지역별", "values": []}]
    result = run(tmp_path, [record(), record(table_key="101:DT_B", tbl_id="DT_B", dimensions=empty_dimension)])
    completeness = result["field_completeness"]
    assert completeness["tables_with_zero_value_dimension"] == 1
    assert completeness["cell_query_ready"] == 1
    assert completeness["dimension_value_count_distribution"]["zero_count"] == 1
    # dimensions 자체는 비어 있지 않으므로 필수 필드 결측으로는 세지 않는다.
    assert completeness["required_fields"]["dimensions"]["missing"] == 0


def test_dimension_value_distribution_uses_every_dimension(tmp_path):
    many = [
        {"obj_id": "A", "obj_nm": "지역별", "values": [{"value_id": str(i)} for i in range(3)]},
        {"obj_id": "B", "obj_nm": "연령별", "values": [{"value_id": "1"}]},
    ]
    result = run(tmp_path, [record(dimensions=many)])
    distribution = result["field_completeness"]["dimension_value_count_distribution"]
    assert distribution["count"] == 2
    assert distribution["min"] == 1 and distribution["max"] == 3
    assert result["field_completeness"]["dimensions_per_table_distribution"]["max"] == 2


def test_summarize_distribution_handles_empty_input():
    assert summarize_distribution([]) == {"count": 0}


def test_period_formats_observed_in_catalog_are_accepted():
    assert classify_period("2025", ["년"], CURRENT_YEAR) == []
    assert classify_period("2025", ["5년"], CURRENT_YEAR) == []
    assert classify_period("2025.08", ["월"], CURRENT_YEAR) == []
    assert classify_period("2026 1/4", ["분기"], CURRENT_YEAR) == []
    assert classify_period("2025 2/2", ["반기"], CURRENT_YEAR) == []
    # 일별 통계(예: 노선별 통행료수입)는 YYYYMMDD로 표기된다.
    assert classify_period("20260723", ["일"], CURRENT_YEAR) == []
    # 부정기 통계는 갱신 주기가 없어 어떤 표기와도 양립한다.
    assert classify_period("2025.08", ["부정기"], CURRENT_YEAR) == []
    assert classify_period("2019", ["부정기"], CURRENT_YEAR) == []


def test_population_projection_years_are_not_defects():
    # 장래인구추계는 2072년까지 발표된다. 미래라는 이유만으로 결함 처리하면 안 된다.
    assert classify_period("2072", ["년"], CURRENT_YEAR) == []
    assert classify_period("2027", ["년"], CURRENT_YEAR) == []


def test_period_anomalies_are_classified():
    assert "latest_period_missing" in classify_period("", ["년"], CURRENT_YEAR)
    assert "period_types_missing" in classify_period("2025", [], CURRENT_YEAR)
    assert "latest_period_unknown_format" in classify_period("2025-08", ["월"], CURRENT_YEAR)
    assert "latest_period_year_too_old" in classify_period("1234", ["년"], CURRENT_YEAR)
    assert "latest_period_month_out_of_range" in classify_period("2025.13", ["월"], CURRENT_YEAR)
    assert "latest_period_date_out_of_range" in classify_period("20261399", ["일"], CURRENT_YEAR)
    assert "latest_period_fraction_invalid" in classify_period("2025 5/4", ["분기"], CURRENT_YEAR)
    assert "period_type_unknown_label" in classify_period("2025", ["격주"], CURRENT_YEAR)
    # 월 단위 표인데 최신 시점이 연도만 있으면 기간 파싱이 조용히 어긋난다.
    assert "latest_period_shape_conflicts_period_types" in classify_period("2025", ["월"], CURRENT_YEAR)


def test_horizon_bounds_flag_only_implausible_years():
    # 기본 50년 폭 밖(2076 초과)만 파싱 오류 후보로 본다.
    assert "latest_period_year_beyond_horizon" in classify_period("2099", ["년"], CURRENT_YEAR)
    assert "latest_period_year_beyond_horizon" not in classify_period("2072", ["년"], CURRENT_YEAR)
    # 폭을 좁히면 추계 연도도 걸린다. 임계값이 실제로 적용되는지 확인한다.
    assert "latest_period_year_beyond_horizon" in classify_period("2072", ["년"], CURRENT_YEAR, horizon_years=5)


def test_period_problem_rows_are_recorded(tmp_path):
    result = run(tmp_path, [record(), record(table_key="101:DT_B", tbl_id="DT_B", latest_period="2025-08")])
    assert result["field_completeness"]["period_problem_counts"]["latest_period_unknown_format"] == 1
    assert len(result["rows"]["period_problems"]) == 1
    assert result["rows"]["period_problems"][0]["table_key"] == "101:DT_B"


def test_parse_failures_are_reported_not_dropped(tmp_path):
    catalog = tmp_path / "catalog.jsonl"
    catalog.write_text(
        json.dumps(record(), ensure_ascii=False) + "\n" + "{broken json\n" + "[1, 2]\n",
        encoding="utf-8",
    )
    result = audit(catalog, None, CURRENT_YEAR)
    assert result["record_totals"]["total_records"] == 1
    assert result["record_totals"]["parse_failures"] == 2
    reasons = [row["reason"] for row in result["rows"]["parse_failures"]]
    assert reasons[0].startswith("invalid_json")
    assert reasons[1] == "not_object"


def test_collection_failures_are_extracted_from_raw_meta(tmp_path):
    raw_meta = [
        {"table_key": "101:DT_1B040A3", "org_id": "101", "tbl_id": "DT_1B040A3", "status": "ok", "fetched_at": "2026-07-23T01:00:00+00:00"},
        {"table_key": "101:DT_B", "org_id": "101", "tbl_id": "DT_B", "status": "error", "fetched_at": "2026-07-24T01:00:00+00:00", "error": "timeout"},
    ]
    result = run(tmp_path, [record()], raw_meta=raw_meta)
    summary = result["collection_record"]
    assert summary["available"] is True
    assert summary["status_counts"] == {"error": 1, "ok": 1}
    assert summary["fetched_at_min"] == "2026-07-23T01:00:00+00:00"
    failures = result["rows"]["collection_failures"]
    assert len(failures) == 1 and failures[0]["table_key"] == "101:DT_B" and failures[0]["error"] == "timeout"


def test_absent_raw_meta_is_unknown_not_zero_failures(tmp_path):
    # 기록이 없으면 '실패가 없었다'가 아니라 '실패 여부를 모른다'로 보고해야 한다.
    result = run(tmp_path, [record()])
    assert result["collection_record"]["available"] is False
    assert result["rows"]["collection_failures"] == []


def test_limit_restricts_audited_records(tmp_path):
    result = run(tmp_path, [record(), record(table_key="101:DT_B", tbl_id="DT_B")])
    catalog = tmp_path / "catalog.jsonl"
    limited = audit(catalog, None, CURRENT_YEAR, limit=1)
    assert result["record_totals"]["total_records"] == 2
    assert limited["record_totals"]["total_records"] == 1


def meta_record(**overrides):
    """수집 원본(meta) 스키마. catalog와 달리 values(id/nm/up_id/sn)를 갖는다."""
    base = {
        "table_key": "101:DT_1B040A3",
        "org_id": "101",
        "tbl_id": "DT_1B040A3",
        "tbl_nm": "행정구역별 인구",
        "status": "ok",
        "fetched_at": "2026-07-23T01:00:00+00:00",
        "items": [{"itm_id": "T1", "itm_nm": "총인구", "unit_nm": "명"}],
        "dimensions": [{
            "obj_id": "A", "obj_nm": "지역별", "value_count": 2,
            "values": [{"id": "11", "nm": "서울특별시", "up_id": None, "sn": "1"},
                       {"id": "26", "nm": "부산광역시", "up_id": None, "sn": "2"}],
        }],
        "units": ["명"],
        "unit_source": "itm",
        "periods": [{"prd_se": "년", "start": "1992", "end": "2025"}],
        "period_types": ["년"],
        "latest_period": "2025",
    }
    base.update(overrides)
    return base


def test_meta_schema_recognizes_structured_values(tmp_path):
    """meta 스키마에는 value_id가 있으므로 셀 조회 준비가 된 것으로 판정돼야 한다."""
    catalog = write_catalog(tmp_path / "meta.jsonl", [meta_record()])
    result = audit(catalog, None, CURRENT_YEAR, schema="meta")
    completeness = result["field_completeness"]
    assert completeness["dimension_value_representation"]["structured_values_array"] == 1
    assert completeness["dimension_value_representation"]["value_count_only"] == 0
    assert completeness["cell_query_ready"] == 1
    assert completeness["rows_with_any_required_missing"] == 0


def test_meta_schema_uses_tbl_nm_not_tbl_name(tmp_path):
    # catalog 스키마로 meta를 읽으면 tbl_name이 없어 오탐이 난다. 프로파일이 실제로 갈리는지 본다.
    catalog = write_catalog(tmp_path / "meta.jsonl", [meta_record()])
    as_meta = audit(catalog, None, CURRENT_YEAR, schema="meta")
    as_catalog = audit(catalog, None, CURRENT_YEAR, schema="catalog")
    assert as_meta["field_completeness"]["required_fields"]["tbl_nm"]["missing"] == 0
    assert as_catalog["field_completeness"]["required_fields"]["tbl_name"]["missing"] == 1
    assert as_meta["field_completeness"]["cell_query_ready"] == 1
    assert as_catalog["field_completeness"]["cell_query_ready"] == 0


def test_meta_schema_observes_its_own_optional_fields(tmp_path):
    catalog = write_catalog(tmp_path / "meta.jsonl", [meta_record(units=[], unit_source="")])
    result = audit(catalog, None, CURRENT_YEAR, schema="meta")
    observed = result["field_completeness"]["observed_fields"]
    assert observed["units"]["missing"] == 1
    assert observed["unit_source"]["missing"] == 1
    assert observed["periods"]["missing"] == 0


def test_compare_value_counts_confirms_faithful_derivation(tmp_path):
    """catalog의 value_count가 meta의 실제 값 개수와 같으면 일관으로 판정한다."""
    catalog = write_catalog(tmp_path / "catalog.jsonl", [record(
        dimensions=[{"obj_id": "A", "obj_nm": "지역별", "value_count": 2}])])
    meta = write_catalog(tmp_path / "meta.jsonl", [meta_record()])
    check = compare_value_counts(catalog, meta)
    assert check["consistent"] is True
    assert check["matched"] == 1 and check["mismatched"] == 0
    assert check["meta_dimensions_with_values"] == 1


def test_compare_value_counts_reports_mismatch_with_examples(tmp_path):
    catalog = write_catalog(tmp_path / "catalog.jsonl", [record(
        dimensions=[{"obj_id": "A", "obj_nm": "지역별", "value_count": 99},
                    {"obj_id": "Z", "obj_nm": "없는축", "value_count": 1}])])
    meta = write_catalog(tmp_path / "meta.jsonl", [meta_record()])
    check = compare_value_counts(catalog, meta)
    assert check["consistent"] is False
    assert check["mismatched"] == 1
    assert check["catalog_dimensions_absent_from_meta"] == 1
    example = check["mismatch_examples"][0]
    assert example["catalog_value_count"] == 99 and example["meta_values_length"] == 2


def test_write_outputs_records_schema_and_cross_check(tmp_path):
    result = run(tmp_path, [record()])
    freeze = {"path": "catalog.jsonl", "bytes": 1, "sha256": "abc", "modified_at": "2026-08-07T00:00:00+00:00"}
    manifest = write_outputs(result, tmp_path / "out", tmp_path / "catalog.jsonl", None, freeze,
                             schema="meta", value_count_check={"consistent": True})
    assert manifest["schema"] == "meta"
    assert manifest["value_count_cross_check"] == {"consistent": True}


def test_write_outputs_creates_every_required_artifact(tmp_path):
    result = run(tmp_path, [record(), record(), record(table_key="999:WRONG")])
    output_dir = tmp_path / "audit"
    freeze = {"path": "catalog.jsonl", "bytes": 1, "sha256": "abc", "modified_at": "2026-08-07T00:00:00+00:00"}
    manifest = write_outputs(result, output_dir, tmp_path / "catalog.jsonl", None, freeze)

    expected = {
        "manifest.json", "duplicate_table_keys.jsonl", "invalid_table_keys.jsonl",
        "missing_required_fields.jsonl", "collection_failures.jsonl",
        "period_problems.jsonl", "field_completeness.json",
    }
    assert expected <= {path.name for path in output_dir.iterdir()}
    assert manifest["input"]["sha256"] == "abc"
    assert manifest["record_totals"]["duplicate_table_keys"] == 1
    # 결함이 없는 산출물도 빈 파일로 남겨 '측정했으나 없음'을 '측정 안 함'과 구분한다.
    assert (output_dir / "collection_failures.jsonl").read_text(encoding="utf-8") == ""
    duplicate_lines = (output_dir / "duplicate_table_keys.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(duplicate_lines) == 1 and json.loads(duplicate_lines[0])["occurrences"] == 2
