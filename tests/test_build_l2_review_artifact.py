import json

from src.develop.build_l2_review_artifact import (
    SOURCE_REGION_SUBTYPES,
    build_l2_review_artifact,
)


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_l2_review_artifact_keeps_human_fields_blank_and_suggestions_separate(
    tmp_path,
):
    sentence_rows = [[
        "문장검토ID", "article_idx", "sentence_id", "기사제목",
        "원문 문장", "자동 value candidates",
    ]]
    claim_rows = [[
        "article_idx", "sentence_id", "검증대상 gold", "indicator gold",
        "period gold", "target value", "candidate span ID", "검토메모",
        "원문 문장",
    ]]
    scaffold = []
    for index in range(117):
        review_id = f"1-S{index:03d}"
        sentence_rows.append([
            review_id, "1", index, "제목",
            f"지난해 고용률은 {index}%였다.", f"{index}%",
        ])
        reason = (
            "MULTI_INDICATOR_EXPLICIT"
            if index < 28 else "REGION_INHERITANCE_REVIEW_REQUIRED"
        )
        source_region_gold = (
            "OUT_OF_SCOPE_UNSPECIFIED"
            if 87 <= index < 96
            else "MIXED"
            if index == 96
            else "NO_VERIFIABLE_NUMERIC_CLAIM"
            if index == 97
            else "OFFICIAL_AGGREGATE"
            if index >= 98
            else "UNLABELED"
        )
        scaffold.append({
            "sentence_review_id": review_id,
            "article_idx": "1",
            "sentence_id": index,
            "derivation_status": reason,
            "requires_human_region_review": index < 87,
            "source_region_gold": source_region_gold,
            "indicator_scope_gold": ["고용률"] if index >= 98 else [],
        })
        claim_rows.append([
            "1", index,
            "NOT_CLAIM" if 87 <= index < 98 else "KOSIS_CANDIDATE",
            "고용률",
            "지난해", f"{index}%", f"s{index}:value", "",
            f"지난해 고용률은 {index}%였다.",
        ])
    snapshot = {
        "matrices": {
            "sentence_review": sentence_rows,
            "claim_gold": claim_rows,
        },
    }
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )
    scaffold_path = tmp_path / "scaffold.jsonl"
    _write_jsonl(scaffold_path, scaffold)
    article_path = tmp_path / "input.jsonl"
    _write_jsonl(article_path, [{
        "article_idx": "1",
        "date": "2025-07-01",
    }])

    human, suggestions, context, contract = build_l2_review_artifact(
        snapshot_path, scaffold_path, article_path
    )

    assert len(human) == 98
    assert len(context) == 117
    assert contract["review_reason_counts"] == {
        "MULTI_INDICATOR_BOUNDARY": 28,
        "CONTEXT_REGION_INHERITANCE": 59,
        "OUT_OF_SCOPE_SUBTYPE": 9,
        "REGION_CONFLICT_RESOLUTION": 2,
    }
    assert contract["source_region_subtypes"] == list(SOURCE_REGION_SUBTYPES)
    assert contract["id_uniqueness_scope"] == "article"
    assert contract["scope_id_format"] == "{article_idx}-SC{nn}"
    assert contract["region_id_format"] == "{article_idx}-R{nn}"
    assert contract["context_row_kind_counts"] == {
        "검토대상": 98,
        "자동확정": 19,
    }
    assert human[0]["value_candidate_span_ids"] == "0%=s0:value"
    assert human[0]["indicator_scopes_json"] == ""
    assert human[0]["source_regions_json"] == ""
    assert human[0]["period_contexts_json"] == ""
    assert human[0]["label_provenance"] == "UNREVIEWED"
    assert suggestions[0]["label_provenance"] == "AUTO_DERIVED"
    assert suggestions[0]["period_contexts_suggestion"][0][
        "period_absolute_suggestion"
    ] == "2024"
    assert suggestions[-1]["indicator_scopes_suggestion"] == []
    assert context[-1]["scope_id"] == "1-SC117"
    assert context[-1]["region_id"] == "1-R117"
    assert context[-1]["source_subtype"] == "공식집계"
