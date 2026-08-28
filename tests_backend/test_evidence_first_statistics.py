from pathlib import Path

from backend.develop_verify_service import _sentence_segment


def test_backend_projects_evidence_answer_as_primary_segment_text():
    answer = {
        "verdict": "REFUTED",
        "explanation": "legacy internal explanation",
        "evidence_answer": {
            "truth_mode": "CURRENT_RELEASE",
            "text": "현재 KOSIS 통계표에서는 2026년 4월 값이 24,521명입니다.",
        },
    }
    segment = _sentence_segment(
        0,
        "기사 문장",
        [],
        live=True,
        answer_for_sentence={0: answer},
        ledger_for_sentence={},
    )
    assert segment["evidence_answer"]["truth_mode"] == "CURRENT_RELEASE"
    assert segment["answer"] == answer["evidence_answer"]["text"]
    assert segment["verdict"] == "mismatch"


def test_frontend_does_not_expose_legacy_verdict_words_as_display_labels():
    source = (Path(__file__).parents[1] / "frontend" / "src" / "ChatApp.jsx").read_text(encoding="utf-8")
    assert "일치" not in source
    assert "불일치" not in source
    assert "현재 통계 근거" in source
