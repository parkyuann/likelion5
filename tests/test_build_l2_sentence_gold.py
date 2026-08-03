import json

from src.develop.build_l2_sentence_gold import build_l2_sentence_gold


def test_build_l2_sentence_gold_marks_explicit_mixed_and_inheritance_rows(tmp_path):
    snapshot = {
        "artifact_status": "HUMAN_ADJUDICATED",
        "matrices": {
            "sentence_review": [
                {"value": ["문장검토ID", "article_idx", "sentence_id", "기사제목", "원문 문장", "자동 value candidates", "문장검토상태"]},
                {"value": ["1-S000", "1", 0, "제목", "문장0", "1%", "완료"]},
                {"value": ["1-S001", "1", 1, "제목", "문장1", None, "완료"]},
                {"value": ["1-S002", "1", 2, "제목", "문장2", "2%, 3%", "완료"]},
            ],
            "claim_gold": [
                {"value": ["article_idx", "sentence_id", "검증대상 gold", "indicator gold", "period gold", "population gold"]},
                {"value": ["1", 0, "KOSIS_CANDIDATE", "고용률", "지난해", "근로자"]},
                {"value": ["1", 2, "KOSIS_CANDIDATE", "생산", "1분기", "전체 산업"]},
                {"value": ["1", 2, "KOSIS_CANDIDATE", "판매", "1분기", "전체 산업"]},
                {"value": ["1", 2, "OUT_OF_SCOPE", "목표", "올해", "없음"]},
            ],
        },
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    rows, report = build_l2_sentence_gold(path)

    assert rows[0]["indicator_scope_gold"] == ["고용률"]
    assert rows[0]["source_region_gold"] == "OFFICIAL_AGGREGATE"
    assert rows[1]["derivation_status"] == "REGION_INHERITANCE_REVIEW_REQUIRED"
    assert rows[2]["derivation_status"] == "MULTI_INDICATOR_EXPLICIT"
    assert rows[2]["source_region_gold"] == "MIXED"
    assert report["sentence_rows"] == 3
    assert report["human_region_review_rows"] == 2
