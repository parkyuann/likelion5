from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from backend import develop_verify_service as service

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.news_verification.runtime.r4c1_claim_core_v2 import build_claim_core_monthly_v2h


def test_backend_explicit_date_is_client_assertion_and_hint_is_not_trusted(monkeypatch):
    captured = {}

    class FakeStageError(RuntimeError):
        pass

    def fake_run_trace(*, articles_path, output_root, stage, **_kwargs):
        output_root.mkdir(parents=True, exist_ok=True)
        article = json.loads(articles_path.read_text(encoding="utf-8").splitlines()[0])
        captured.setdefault("article", article)
        if stage == "l1":
            (output_root / "01_value_candidates.jsonl").write_text(
                json.dumps({"kind": "value_unit"}) + "\n", encoding="utf-8"
            )
        if stage == "layers":
            body = article["article_text"]
            (output_root / "01_sentences.jsonl").write_text(
                json.dumps({
                    "sentence_id": 0, "text": body,
                    "char_start": 0, "char_end": len(body),
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (output_root / "03_routed.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setenv("EVIDENCE_FIRST_STATISTICS_SHADOW_ENABLED", "true")
    monkeypatch.setattr(service, "_load_trace_runner", lambda: (fake_run_trace, FakeStageError))
    monkeypatch.setattr(service, "pipeline_live_stage_enabled", lambda: False)
    body = "  지난 4월 출생아는 2만4521명이다.\n내부 공백은  유지한다.  "
    canonical = body.strip()

    result = service.verify_article_develop(
        body, date="2026-08-24", date_source="untrusted-arbitrary-hint"
    )

    receipt = captured["article"]["article_date_provenance"]
    assert receipt == {
        "date_source": "client_asserted",
        "source_path": "backend_request",
        "date_field": "date",
        "article_text_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    assert captured["article"]["article_text"] == canonical
    assert result["status"] == "structured_only"


def test_monthly_date_clarification_provenance_is_accepted_and_preserved():
    sentence = "지난 4월 출생아는 100명이다."
    article_sha = hashlib.sha256(sentence.encode("utf-8")).hexdigest()
    row = {
        "article_idx": "clarified-monthly",
        "article_text": sentence,
        "sentence_text": sentence,
        "article_sentence_id": 0,
        "article_date": "2026-08-26",
        "value_span_id": "value-1",
        "value_text": "100",
        "value_unit": "명",
        "period_raw": "지난 4월",
        "indicator_evidence": {
            "sentence_id": 0,
            "source_char_start": 0,
            "source_char_end": len(sentence),
            "source_span_text": sentence,
            "model_indicator_label": "출생아",
        },
        "retrieval_fields": {
            "indicator": "출생아 수",
            "period_absolute": "2026-04",
            "period": {"measurement": {"absolute": "2026-04"}},
        },
        "article_date_provenance": {
            "date_source": "user_feedback",
            "source_path": "clarification_context",
            "date_field": "date",
            "article_text_sha256": article_sha,
            "answer_sha256": "a" * 64,
        },
    }

    core = build_claim_core_monthly_v2h(row)

    assert core.provenance["article_date_provenance"]["date_source"] == "user_feedback"
    assert core.provenance["article_date_provenance"]["source_path"] == "clarification_context"
    assert core.provenance["anchor_receipt"]["date_source"] == "user_feedback"
