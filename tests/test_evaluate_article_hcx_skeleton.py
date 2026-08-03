import json

from src.develop.evaluate_article_hcx_skeleton import _key, evaluate


def test_skeleton_key_removes_measurement_suffix_without_changing_indicator_subject():
    assert _key("전산업 생산지수 전월비 증감률") == "전산업생산지수"
    assert _key("서비스 가격 상승률") == "서비스가격"
    assert _key("저축은행 연체율") == "저축은행연체율"
    assert _key("제조업 생산 증가율") == "제조업생산"
    assert _key("건설 수주액 감소율") == "건설수주액"


def test_skeleton_evaluation_treats_null_bound_period_as_a_miss(tmp_path):
    article_dir = tmp_path / "기사_1"
    article_dir.mkdir()
    (article_dir / "raw.jsonl").write_text(json.dumps({
        "article_idx": "1",
        "semantic_prediction": {"claims": [{
            "indicator_norm": "고용률",
            "observation_sentence_ids": [0],
        }]},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (article_dir / "pass_observations.jsonl").write_text(json.dumps({
        "semantic_claim": {"indicator_norm": "고용률"},
        "validation": {
            "value_span": {"text": "45.1%", "sentence_id": 0},
            "measurement_type": "LEVEL",
            "period_span": None,
        },
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (article_dir / "input.jsonl").write_text(json.dumps({
        "article_idx": "1",
        "article_text": "지난달 고용률은 45.1%였다.",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    result = evaluate(tmp_path, [{
        "fixture_id": "1-01",
        "article_idx": "1",
        "eligibility": "KOSIS_CANDIDATE",
        "indicator_norm": "고용률",
        "value_text": "45.1%",
        "value_sentence_id": 0,
        "measurement_type": "LEVEL",
        "period": {"text": "지난달", "sentence_id": 0},
    }])

    assert result["metrics"]["skeleton_match"]["matched"] == 1
    assert result["metrics"]["source_value_match"]["matched"] == 1
    assert result["metrics"]["period_match"]["matched"] == 0
    assert result["metrics"]["source_period_match"]["matched"] == 0


def test_skeleton_evaluation_requires_a_matched_value_even_without_gold_period(tmp_path):
    article_dir = tmp_path / "기사_1"
    article_dir.mkdir()
    (article_dir / "raw.jsonl").write_text(json.dumps({
        "article_idx": "1",
        "semantic_prediction": {"claims": []},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (article_dir / "pass_observations.jsonl").write_text("", encoding="utf-8")
    (article_dir / "input.jsonl").write_text(json.dumps({
        "article_idx": "1",
        "article_text": "건설 수주액은 6.9% 감소했다.",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    result = evaluate(tmp_path, [{
        "fixture_id": "1-01",
        "article_idx": "1",
        "eligibility": "KOSIS_CANDIDATE",
        "indicator_norm": "건설 수주액 전년동월비 증감률",
        "value_text": "6.9%",
        "value_sentence_id": 0,
        "measurement_type": "CHANGE_RATE",
        "period": None,
    }])

    assert result["metrics"]["period_match"]["matched"] == 0
    assert result["metrics"]["source_value_match"]["matched"] == 0


def test_skeleton_evaluation_reads_flat_multi_article_runner_output(tmp_path):
    raw_rows = [
        {
            "article_idx": article_idx,
            "semantic_prediction": {"claims": [{
                "indicator_norm": indicator,
                "observation_sentence_ids": [0],
            }]},
        }
        for article_idx, indicator in (("1", "고용률"), ("2", "실업률"))
    ]
    passed_rows = [
        {
            "article_idx": article_idx,
            "semantic_claim": {"indicator_norm": indicator},
            "validation": {
                "value_span": {"text": value, "sentence_id": 0},
                "measurement_type": "LEVEL",
                "period_span": None,
            },
        }
        for article_idx, indicator, value in (
            ("1", "고용률", "45.1%"),
            ("2", "실업률", "3.2%"),
        )
    ]
    (tmp_path / "raw.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_rows),
        encoding="utf-8",
    )
    (tmp_path / "pass_observations.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in passed_rows),
        encoding="utf-8",
    )
    (tmp_path / "input.jsonl").write_text(
        "".join(json.dumps({
            "article_idx": article_idx,
            "article_text": f"{indicator}은 {value}였다.",
        }, ensure_ascii=False) + "\n" for article_idx, indicator, value in (
            ("1", "고용률", "45.1%"),
            ("2", "실업률", "3.2%"),
        )),
        encoding="utf-8",
    )

    result = evaluate(tmp_path, [
        {
            "fixture_id": f"{article_idx}-01",
            "article_idx": article_idx,
            "eligibility": "KOSIS_CANDIDATE",
            "indicator_norm": indicator,
            "value_text": value,
            "value_sentence_id": 0,
            "measurement_type": "LEVEL",
            "period": None,
        }
        for article_idx, indicator, value in (
            ("1", "고용률", "45.1%"),
            ("2", "실업률", "3.2%"),
        )
    ])

    assert result["metrics"]["skeleton_match"]["matched"] == 2
    assert result["metrics"]["raw_target_candidate_match"]["matched"] == 0
    assert result["metrics"]["source_value_match"]["matched"] == 2
    assert result["metrics"]["value_match"]["matched"] == 2
    assert result["metrics"]["source_measurement_type_match"]["matched"] == 2
    assert result["metrics"]["source_period_match"]["matched"] == 2


def test_skeleton_evaluation_reports_source_field_metrics_independent_of_indicator_exact(tmp_path):
    (tmp_path / "raw.jsonl").write_text(json.dumps({
        "article_idx": "1",
        "semantic_prediction": {"claims": [{
            "indicator_norm": "사과 가격 상승률",
            "observation_sentence_ids": [0],
        }]},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "pass_observations.jsonl").write_text(json.dumps({
        "article_idx": "1",
        "semantic_claim": {"indicator_norm": "사과 가격 상승률"},
        "validation": {
            "value_span": {"text": "21.6%", "sentence_id": 0},
            "measurement_type": "CHANGE_RATE",
            "period_span": {"text": "지난달"},
        },
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "input.jsonl").write_text(json.dumps({
        "article_idx": "1",
        "article_text": "지난달 사과는 21.6% 올랐다.",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    result = evaluate(tmp_path, [{
        "fixture_id": "1-01",
        "article_idx": "1",
        "eligibility": "KOSIS_CANDIDATE",
        "indicator_norm": "사과 상승률",
        "value_text": "21.6%",
        "value_sentence_id": 0,
        "measurement_type": "CHANGE_RATE",
        "period": {"text": "지난달", "sentence_id": 0},
    }])

    assert result["metrics"]["value_match"]["matched"] == 0
    assert result["metrics"]["measurement_type_match"]["matched"] == 0
    assert result["metrics"]["period_match"]["matched"] == 0
    assert result["metrics"]["source_value_match"]["matched"] == 1
    assert result["metrics"]["source_measurement_type_match"]["matched"] == 1
    assert result["metrics"]["source_period_match"]["matched"] == 1
