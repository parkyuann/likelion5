import json

import pandas as pd

from src.build_context_referent_fixture import build_fixture


def test_build_fixture_contains_only_unresolved_context_cases_with_evidence_fields():
    frame = pd.DataFrame([{
        "기사제목": "보험 기사", "작성일": "2026-07-24", "검색 구분 레이블": "test",
        "본문_정제": "손해보험과 생명보험 계약이 늘었다. 보험료는 3% 상승했다.",
    }])
    rows = build_fixture(frame)
    assert len(rows) == 1
    assert rows[0]["rule_context_status"] == "REFERENT_AMBIGUOUS"
    assert set(json.loads(rows[0]["candidate_terms_json"])) == {"손해보험", "생명보험"}
    assert len(json.loads(rows[0]["context_window_json"])) == 2
