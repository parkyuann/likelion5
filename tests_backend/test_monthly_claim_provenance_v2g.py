from __future__ import annotations

import hashlib
import json

from backend import develop_verify_service as service


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
