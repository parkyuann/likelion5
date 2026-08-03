from src.develop import run_l2_segmentation


ARTICLE = {
    "article_idx": "380",
    "title": "단기 근로자",
    "article_text": "빵 물가 상승률은 38.5%다. 통계청에 따르면 취업자는 2909만명이다.",
}


def _stub(resolved, calls=None):
    def call(title, body, *, api_key, model="HCX-007"):
        if calls is not None:
            calls.append(title)
        return resolved, {"totalTokens": 100}, 12.5
    return call


def test_run_flattens_sentences_with_article_idx(monkeypatch):
    resolved = {
        "sentences": [
            {"sentence_id": 0, "text": "a", "indicator_scopes": [],
             "source_region": {"dominance": "지배 없음"}, "period_context": {}},
        ],
        "missing_sentence_ids": [],
        "unresolved_spans": 0,
    }
    monkeypatch.setattr(
        run_l2_segmentation, "call_hcx_l2_segmentation", _stub(resolved)
    )

    predictions, manifest = run_l2_segmentation.run(
        [ARTICLE], api_key="x", pause_seconds=0
    )

    assert predictions[0]["article_idx"] == "380"
    assert manifest["sentences_predicted"] == 1
    assert manifest["total_tokens"] == 100
    assert manifest["errors"] == []


def test_run_records_missing_sentences_and_unresolved_spans(monkeypatch):
    resolved = {
        "sentences": [],
        "missing_sentence_ids": [1],
        "unresolved_spans": 2,
    }
    monkeypatch.setattr(
        run_l2_segmentation, "call_hcx_l2_segmentation", _stub(resolved)
    )

    _, manifest = run_l2_segmentation.run(
        [ARTICLE], api_key="x", pause_seconds=0
    )

    kinds = {error["kind"] for error in manifest["errors"]}
    assert kinds == {"MISSING_SENTENCES", "UNRESOLVED_SPANS"}


def test_run_retries_then_records_failure(monkeypatch):
    attempts = []

    def failing(title, body, *, api_key, model="HCX-007"):
        attempts.append(1)
        raise RuntimeError("429 rate limited")

    monkeypatch.setattr(
        run_l2_segmentation, "call_hcx_l2_segmentation", failing
    )

    _, manifest = run_l2_segmentation.run(
        [ARTICLE], api_key="x", retries=2, pause_seconds=0
    )

    assert len(attempts) == 3
    assert manifest["errors"][0]["kind"] == "CALL_FAILED"
    assert "429" in manifest["errors"][0]["detail"]
