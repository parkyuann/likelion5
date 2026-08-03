"""Evaluate a frozen article-level HCX holdout after human adjudication.

The XLSX workbook is imported by the spreadsheet artifact runtime into an
immutable JSON snapshot.  This module intentionally consumes that snapshot,
validates it against the pre-evaluation fixture, joins one fresh HCX run, and
reports candidate-level claim detection, KOSIS routing, and semantic fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .apply_article_hcx_blind_review import (
    _f1,
    _role_metric,
    _safe_ratio,
    _scalar_metric,
    normalize_exact,
)
from .article_claim_pipeline import build_span_candidates


SENTENCE_HEADERS = [
    "문장검토ID",
    "article_idx",
    "sentence_id",
    "기사제목",
    "원문 문장",
    "자동 value candidates",
    "검증가능 claim 수",
    "비대상 수치 claim 수",
    "문장검토상태",
    "누락claim메모",
]

CLAIM_HEADERS = [
    "gold_id",
    "article_idx",
    "sentence_id",
    "원문 문장",
    "target value",
    "candidate span ID",
    "행 출처",
    "claim 여부",
    "검증대상 gold",
    "indicator gold",
    "measurement gold",
    "period gold",
    "population gold",
    "item gold",
    "dimension gold",
    "값-pairing",
    "최종상태",
    "검토메모",
]

ELIGIBILITY = {"KOSIS_CANDIDATE", "OUT_OF_SCOPE", "NOT_CLAIM"}
MEASUREMENT_TYPES = {"LEVEL", "CHANGE_RATE", "CHANGE_POINT", "INDEX_LEVEL"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _explicit_empty(value: object) -> str:
    text = _text(value)
    return "" if text == "없음" else text


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}: boolean is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    text = _text(value)
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"{field}: expected non-negative integer, got {value!r}")
    return int(text)


def _split_role(value: object) -> list[str]:
    text = _explicit_empty(value)
    if not text:
        return []
    values = [part.strip() for part in text.split(",")]
    if not all(values):
        raise ValueError(f"invalid role list: {value!r}")
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate role value: {value!r}")
    return values


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _rows(matrix: object, headers: list[str], *, name: str) -> list[list[Any]]:
    if not isinstance(matrix, list) or len(matrix) < 2:
        raise ValueError(f"{name}: matrix is missing or empty")
    if matrix[0] != headers:
        raise ValueError(f"{name}: unexpected headers {matrix[0]!r}")
    rows = [
        row
        for row in matrix[1:]
        if isinstance(row, list) and any(cell not in (None, "") for cell in row)
    ]
    if any(len(row) != len(headers) for row in rows):
        raise ValueError(f"{name}: row width mismatch")
    return rows


def parse_adjudication_snapshot(
    snapshot: dict[str, Any],
    *,
    fixture_root: Path,
    verify_workbook: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    matrices = snapshot.get("matrices")
    if not isinstance(matrices, dict):
        raise ValueError("adjudication matrices are missing")
    sentence_matrix = _rows(
        matrices.get("sentence_review"),
        SENTENCE_HEADERS,
        name="sentence_review",
    )
    claim_matrix = _rows(
        matrices.get("claim_gold"),
        CLAIM_HEADERS,
        name="claim_gold",
    )

    workbook_path = Path(_text(snapshot.get("workbook_path")))
    workbook_sha256 = _text(snapshot.get("workbook_sha256"))
    if verify_workbook:
        if not workbook_path.is_file():
            raise ValueError(f"review workbook is unavailable: {workbook_path}")
        actual_hash = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
        if actual_hash != workbook_sha256:
            raise ValueError(
                "review workbook hash mismatch: "
                f"snapshot={workbook_sha256}, actual={actual_hash}"
            )

    fixture_sentences = {
        (_text(row.get("article_idx")), int(row["sentence_id"])): row
        for row in _load_jsonl(fixture_root / "sentences.jsonl")
    }
    fixture_candidates = {
        (_text(row.get("article_idx")), _text(row.get("span_id"))): row
        for row in _load_jsonl(fixture_root / "value_candidates.jsonl")
    }

    sentence_records: list[dict[str, Any]] = []
    seen_sentence_ids: set[str] = set()
    for sheet_row, row in enumerate(sentence_matrix, start=2):
        review_id = _text(row[0])
        article_idx = _text(row[1])
        sentence_id = _integer(row[2], field=f"{review_id}.sentence_id")
        expected_id = f"{article_idx}-S{sentence_id:03d}"
        if review_id != expected_id:
            raise ValueError(f"{review_id}: expected sentence review ID {expected_id}")
        if review_id in seen_sentence_ids:
            raise ValueError(f"duplicate sentence review ID: {review_id}")
        seen_sentence_ids.add(review_id)
        fixture = fixture_sentences.get((article_idx, sentence_id))
        if fixture is None:
            raise ValueError(f"{review_id}: sentence is absent from frozen fixture")
        if _text(row[4]) != _text(fixture.get("text")):
            raise ValueError(f"{review_id}: sentence text changed after freeze")
        if _text(row[8]) != "완료":
            raise ValueError(f"{review_id}: sentence review is not complete")
        sentence_records.append({
            "review_id": review_id,
            "article_idx": article_idx,
            "sentence_id": sentence_id,
            "title": _text(row[3]),
            "sentence": _text(row[4]),
            "automatic_value_candidates": _text(row[5]),
            "kosis_claim_count": _integer(
                row[6],
                field=f"{review_id}.kosis_claim_count",
            ),
            "out_of_scope_claim_count": _integer(
                row[7],
                field=f"{review_id}.out_of_scope_claim_count",
            ),
            "review_note": _text(row[9]),
            "source": {"sheet": "전체문장검토", "row": sheet_row},
        })
    if set(fixture_sentences) != {
        (row["article_idx"], row["sentence_id"]) for row in sentence_records
    }:
        raise ValueError("sentence review does not cover the frozen fixture exactly")

    claim_records: list[dict[str, Any]] = []
    seen_gold_ids: set[str] = set()
    placeholder_rows = 0
    for sheet_row, row in enumerate(claim_matrix, start=2):
        gold_id = _text(row[0])
        if not gold_id or gold_id in seen_gold_ids:
            raise ValueError(f"missing or duplicate gold ID: {gold_id!r}")
        seen_gold_ids.add(gold_id)
        article_idx = _text(row[1])
        row_source = _text(row[6])
        eligibility = _text(row[8])
        if row_source not in {"AUTO_VALUE", "ADDED_MISSED"}:
            raise ValueError(f"{gold_id}: invalid row source {row_source!r}")
        if eligibility not in ELIGIBILITY:
            raise ValueError(f"{gold_id}: invalid eligibility {eligibility!r}")
        claim_flag = _text(row[7])
        expected_flag = "NO" if eligibility == "NOT_CLAIM" else "YES"
        if claim_flag != expected_flag:
            raise ValueError(
                f"{gold_id}: claim flag {claim_flag!r} conflicts with {eligibility}"
            )
        expected_status = "확정" if eligibility == "KOSIS_CANDIDATE" else "제외"
        if _text(row[16]) != expected_status:
            raise ValueError(
                f"{gold_id}: final status conflicts with eligibility"
            )
        value_pairing = _text(row[15])
        if value_pairing != claim_flag:
            raise ValueError(
                f"{gold_id}: value pairing must equal claim flag in this fixture"
            )

        sentence_text = _text(row[3])
        target_value = _text(row[4])
        candidate_span_id = _text(row[5])
        if (
            row_source == "ADDED_MISSED"
            and not _text(row[2])
            and not sentence_text
            and not target_value
            and eligibility == "NOT_CLAIM"
        ):
            placeholder_rows += 1
            continue

        sentence_id = _integer(row[2], field=f"{gold_id}.sentence_id")
        fixture_sentence = fixture_sentences.get((article_idx, sentence_id))
        if fixture_sentence is None:
            raise ValueError(f"{gold_id}: sentence is absent from frozen fixture")
        if sentence_text != _text(fixture_sentence.get("text")):
            raise ValueError(f"{gold_id}: sentence text changed after freeze")
        if row_source == "AUTO_VALUE":
            candidate = fixture_candidates.get((article_idx, candidate_span_id))
            if candidate is None:
                raise ValueError(f"{gold_id}: candidate span is absent from fixture")
            if target_value != _text(candidate.get("text")):
                raise ValueError(f"{gold_id}: candidate text changed after freeze")
        elif candidate_span_id:
            raise ValueError(f"{gold_id}: added-missed row cannot carry a span ID")

        measurement = _explicit_empty(row[10])
        if eligibility != "NOT_CLAIM" and measurement not in MEASUREMENT_TYPES:
            raise ValueError(
                f"{gold_id}: invalid measurement type {measurement!r}"
            )
        indicator = _explicit_empty(row[9])
        if eligibility != "NOT_CLAIM" and not indicator:
            raise ValueError(f"{gold_id}: claim requires indicator gold")
        claim_records.append({
            "review_id": gold_id,
            "article_idx": article_idx,
            "sentence_id": sentence_id,
            "source_sentence": sentence_text,
            "target_value": target_value,
            "candidate_span_id": candidate_span_id,
            "row_source": row_source,
            "gold": {
                "claim": claim_flag == "YES",
                "eligibility": eligibility,
                "indicator": indicator,
                "measurement_type": measurement,
                "period": _explicit_empty(row[11]),
                "population": _split_role(row[12]),
                "item": _split_role(row[13]),
                "dimension": _split_role(row[14]),
                "value_pairing": value_pairing,
                "review_note": _text(row[17]),
            },
            "source": {
                "sheet": "claim_gold",
                "row": sheet_row,
                "workbook_path": str(workbook_path),
                "workbook_sha256": workbook_sha256,
            },
        })

    if len(fixture_candidates) != sum(
        row["row_source"] == "AUTO_VALUE" for row in claim_records
    ):
        raise ValueError("AUTO_VALUE rows do not cover frozen candidates exactly")
    if {
        (row["article_idx"], row["candidate_span_id"])
        for row in claim_records
        if row["row_source"] == "AUTO_VALUE"
    } != set(fixture_candidates):
        raise ValueError("AUTO_VALUE candidate IDs differ from frozen fixture")

    gold_by_sentence: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
    for row in claim_records:
        gold_by_sentence[(row["article_idx"], row["sentence_id"])][
            row["gold"]["eligibility"]
        ] += 1
    count_errors: list[str] = []
    for sentence in sentence_records:
        counts = gold_by_sentence[
            (sentence["article_idx"], sentence["sentence_id"])
        ]
        if sentence["kosis_claim_count"] != counts["KOSIS_CANDIDATE"]:
            count_errors.append(f"{sentence['review_id']}: KOSIS count mismatch")
        if sentence["out_of_scope_claim_count"] != counts["OUT_OF_SCOPE"]:
            count_errors.append(f"{sentence['review_id']}: OOS count mismatch")
    if count_errors:
        raise ValueError("sentence/claim reconciliation failed:\n" + "\n".join(
            count_errors
        ))

    return sentence_records, claim_records, {
        "status": "PASS",
        "workbook_path": str(workbook_path),
        "workbook_sha256": workbook_sha256,
        "sentence_rows": len(sentence_records),
        "claim_input_rows": len(claim_matrix),
        "placeholder_rows_excluded": placeholder_rows,
        "evaluation_units": len(claim_records),
        "eligibility_counts": dict(
            Counter(row["gold"]["eligibility"] for row in claim_records)
        ),
        "row_source_counts": dict(Counter(
            row["row_source"] for row in claim_records
        )),
    }


def _role_texts(role_evidence: dict[str, Any], field: str) -> list[str]:
    values: list[str] = []
    for span in role_evidence.get(field, []):
        text = _text(span.get("text") if isinstance(span, dict) else None)
        if text and text not in values:
            values.append(text)
    return values


def _prediction_fields(claim: dict[str, Any], target_span_id: str) -> dict[str, Any]:
    semantic = claim.get("semantic_claim", {})
    validation = claim.get("validation", {})
    scope = claim.get("scope_validation", {})
    observations = [
        observation
        for observation in validation.get("observations", [])
        if _text((observation.get("value_span") or {}).get("span_id"))
        == target_span_id
    ]
    observation = observations[0] if observations else {}
    effective = (
        observation.get("effective_search_fields")
        if isinstance(observation.get("effective_search_fields"), dict)
        else {}
    )
    role_evidence = validation.get("semantic_role_evidence", {})
    return {
        "candidate_class": _text(semantic.get("candidate_class")),
        "classification_reason": _text(
            semantic.get("classification_reason")
        ),
        "indicator": _text(
            effective.get("indicator_norm") or semantic.get("indicator_norm")
        ),
        "measurement_type": _text(observation.get("measurement_type")),
        "period": _text(
            observation.get("period_normalized")
            or (observation.get("period_span") or {}).get("text")
        ),
        "population": (
            list(effective.get("population_terms", []))
            if "population_terms" in effective
            else _role_texts(role_evidence, "population_evidence_spans")
        ),
        "item": (
            list(effective.get("item_terms", []))
            if "item_terms" in effective
            else _role_texts(role_evidence, "item_evidence_spans")
        ),
        "dimension": (
            list(effective.get("dimension_terms", []))
            if "dimension_terms" in effective
            else [
                _text(span.get("text"))
                for span in observation.get("dimension_spans", [])
                if isinstance(span, dict) and _text(span.get("text"))
            ]
        ),
        "semantic_status": _text(
            claim.get("semantic_validation", {}).get("status")
        ),
        "binding_status": _text(validation.get("claim_status")),
        "scope_status": _text(scope.get("claim_status")),
        "action": "PASS" if scope.get("claim_status") == "PASS" else "BLOCKED",
        "observation_matched": bool(observations),
        "semantic_errors": list(
            claim.get("semantic_validation", {}).get("errors", [])
        ),
        "binding_errors": list(validation.get("errors", [])),
        "scope_errors": list(scope.get("errors", [])),
    }


def _target_core(value: object) -> str:
    text = re.sub(r"\([^)]*\)", "", _text(value))
    if re.search(r"(?:분기|월|년).*(?:분기|월|년)", text):
        text = re.sub(r"(?:부터|까지)", "", text)
    return normalize_exact(text)


def apply_run_predictions(
    records: list[dict[str, Any]],
    *,
    fixture_root: Path,
    run_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_inputs = {
        _text(row.get("article_idx")): row for row in _load_jsonl(run_dir / "input.jsonl")
    }
    fixture_inputs = {
        _text(row.get("article_idx")): row
        for row in _load_jsonl(fixture_root / "input.jsonl")
    }
    if set(run_inputs) != set(fixture_inputs):
        raise ValueError("run articles differ from frozen holdout")
    for article_idx, fixture in fixture_inputs.items():
        if hashlib.sha256(
            _text(run_inputs[article_idx].get("article_text")).encode("utf-8")
        ).hexdigest() != _text(fixture.get("article_sha256")):
            raise ValueError(f"article {article_idx}: run text hash differs from freeze")

    candidate_text = {
        (_text(row.get("article_idx")), _text(row.get("span_id"))): _text(
            row.get("text")
        )
        for row in _load_jsonl(fixture_root / "value_candidates.jsonl")
    }
    for candidate_row in _load_jsonl(run_dir / "span_candidates.jsonl"):
        article_idx = _text(candidate_row.get("article_idx"))
        for candidate in candidate_row.get("candidates", []):
            if candidate.get("kind") == "value_unit":
                candidate_text[(
                    article_idx,
                    _text(candidate.get("span_id")),
                )] = _text(candidate.get("text"))
    validation_claims: dict[tuple[str, int], dict[str, Any]] = {}
    for article in _load_jsonl(run_dir / "validation.jsonl"):
        article_idx = _text(article.get("article_idx"))
        for claim in article.get("validation", {}).get("claims", []):
            validation_claims[(article_idx, int(claim["claim_index"]))] = claim

    candidates_by_span: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    all_claims: list[dict[str, Any]] = []
    for raw in _load_jsonl(run_dir / "raw.jsonl"):
        article_idx = _text(raw.get("article_idx"))
        for claim_index, raw_semantic in enumerate(
            raw.get("semantic_prediction", {}).get("claims", [])
        ):
            claim = validation_claims.get((article_idx, claim_index))
            if claim is None:
                raise ValueError(
                    f"article {article_idx} claim {claim_index}: validation missing"
                )
            target_ids = [
                _text(value)
                for value in claim.get("semantic_claim", {}).get(
                    "target_value_span_ids", []
                )
                if _text(value)
            ]
            candidate = {
                "article_idx": article_idx,
                "claim_index": claim_index,
                "raw_semantic_claim": raw_semantic,
                "claim": claim,
                "target_span_ids": target_ids,
            }
            all_claims.append(candidate)
            for span_id in target_ids:
                candidates_by_span[(article_idx, span_id)].append(candidate)

    output: list[dict[str, Any]] = []
    duplicate_matches: list[str] = []
    for source in records:
        row = json.loads(json.dumps(source, ensure_ascii=False))
        matched: list[tuple[dict[str, Any], str, str]] = []
        if row["candidate_span_id"]:
            for candidate in candidates_by_span.get(
                (row["article_idx"], row["candidate_span_id"]),
                [],
            ):
                matched.append((
                    candidate,
                    row["candidate_span_id"],
                    "EXACT_CANDIDATE_SPAN",
                ))
        else:
            target_core = _target_core(row["target_value"])
            if target_core:
                for candidate in all_claims:
                    if candidate["article_idx"] != row["article_idx"]:
                        continue
                    semantic = candidate["claim"].get("semantic_claim", {})
                    context_ids = {
                        int(value)
                        for value in semantic.get("context_sentence_ids", [])
                    }
                    if row["sentence_id"] not in context_ids:
                        continue
                    for span_id in candidate["target_span_ids"]:
                        if (
                            _target_core(candidate_text.get(
                                (row["article_idx"], span_id), ""
                            ))
                            == target_core
                        ):
                            matched.append((
                                candidate,
                                span_id,
                                "CONTEXT_SENTENCE_TARGET_TEXT",
                            ))
        if len(matched) > 1:
            duplicate_matches.append(row["review_id"])
        matched.sort(key=lambda item: (
            item[0]["claim"].get("scope_validation", {}).get("claim_status")
            != "PASS",
            item[0]["claim_index"],
        ))
        if matched:
            selected, target_span_id, match_method = matched[0]
            automatic = _prediction_fields(selected["claim"], target_span_id)
            detected = True
            claim_index: int | None = selected["claim_index"]
        else:
            automatic = {
                "candidate_class": "",
                "classification_reason": "",
                "indicator": "",
                "measurement_type": "",
                "period": "",
                "population": [],
                "item": [],
                "dimension": [],
                "semantic_status": "",
                "binding_status": "",
                "scope_status": "",
                "action": "MISSED",
                "observation_matched": False,
                "semantic_errors": [],
                "binding_errors": [],
                "scope_errors": [],
            }
            detected = False
            claim_index = None
            match_method = "NONE"
            target_span_id = ""
        row["automatic"] = automatic
        row["prediction"] = {
            "detected": detected,
            "claim_index": claim_index,
            "target_span_id": target_span_id,
            "match_method": match_method,
            "match_count": len(matched),
        }
        row["prediction_run"] = str(run_dir)
        output.append(row)

    return output, {
        "run_articles": len(run_inputs),
        "semantic_claims": len(all_claims),
        "unique_target_spans_selected": len(candidates_by_span),
        "duplicate_gold_match_rows": duplicate_matches,
    }


def evaluate_current_candidate_extractor(
    records: list[dict[str, Any]],
    *,
    fixture_root: Path,
) -> dict[str, Any]:
    fixture_inputs = _load_jsonl(fixture_root / "input.jsonl")
    current_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for article in fixture_inputs:
        article_idx = _text(article.get("article_idx"))
        for candidate in build_span_candidates(_text(article.get("article_text"))):
            if candidate.get("kind") == "value_unit":
                current_candidates[(
                    article_idx,
                    _text(candidate.get("span_id")),
                )] = candidate

    matched_candidate_keys: set[tuple[str, str]] = set()
    detected_by_id: dict[str, bool] = {}
    for row in records:
        detected = False
        if row["candidate_span_id"]:
            key = (row["article_idx"], row["candidate_span_id"])
            detected = key in current_candidates
            if detected:
                matched_candidate_keys.add(key)
        else:
            target_core = _target_core(row["target_value"])
            matches = [
                key
                for key, candidate in current_candidates.items()
                if (
                    key[0] == row["article_idx"]
                    and candidate.get("sentence_id") == row["sentence_id"]
                    and _target_core(candidate.get("text")) == target_core
                    and target_core
                )
            ]
            detected = len(matches) == 1
            if detected:
                matched_candidate_keys.add(matches[0])
        detected_by_id[row["review_id"]] = detected

    gold_claim = [row["gold"]["claim"] for row in records]
    detected = [detected_by_id[row["review_id"]] for row in records]
    labelled_tp = sum(p and g for p, g in zip(detected, gold_claim))
    labelled_fp = sum(p and not g for p, g in zip(detected, gold_claim))
    false_negative = sum(not p and g for p, g in zip(detected, gold_claim))
    true_negative = sum(not p and not g for p, g in zip(detected, gold_claim))
    unlabelled_candidates = sorted(set(current_candidates) - matched_candidate_keys)
    false_positive = labelled_fp + len(unlabelled_candidates)
    precision = _safe_ratio(labelled_tp, labelled_tp + false_positive)
    recall = _safe_ratio(labelled_tp, labelled_tp + false_negative)
    return {
        "prediction_definition": (
            "현재 build_span_candidates의 value_unit; ADDED_MISSED는 같은 "
            "문장에서 target text가 정확히 복원될 때만 검출"
        ),
        "true_positive": labelled_tp,
        "false_positive": false_positive,
        "false_positive_labelled_rows": labelled_fp,
        "false_positive_unlabelled_new_candidates": len(unlabelled_candidates),
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "false_negative_review_ids": [
            row["review_id"]
            for row, prediction, gold in zip(records, detected, gold_claim)
            if not prediction and gold
        ],
        "unlabelled_new_candidates": [
            {
                "article_idx": article_idx,
                "span_id": span_id,
                "sentence_id": current_candidates[(article_idx, span_id)].get(
                    "sentence_id"
                ),
                "text": current_candidates[(article_idx, span_id)].get("text"),
            }
            for article_idx, span_id in unlabelled_candidates
        ],
        "current_value_candidate_count": len(current_candidates),
    }


def evaluate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_detected = [
        row["row_source"] == "AUTO_VALUE" for row in records
    ]
    semantic_selected = [row["prediction"]["detected"] for row in records]
    gold_claim = [row["gold"]["claim"] for row in records]
    claim_tp = sum(p and g for p, g in zip(candidate_detected, gold_claim))
    claim_fp = sum(p and not g for p, g in zip(candidate_detected, gold_claim))
    claim_fn = sum(not p and g for p, g in zip(candidate_detected, gold_claim))
    claim_tn = sum(not p and not g for p, g in zip(candidate_detected, gold_claim))
    claim_precision = _safe_ratio(claim_tp, claim_tp + claim_fp)
    claim_recall = _safe_ratio(claim_tp, claim_tp + claim_fn)
    semantic_tp = sum(p and g for p, g in zip(semantic_selected, gold_claim))
    semantic_fp = sum(
        p and not g for p, g in zip(semantic_selected, gold_claim)
    )
    semantic_fn = sum(
        not p and g for p, g in zip(semantic_selected, gold_claim)
    )
    semantic_tn = sum(
        not p and not g for p, g in zip(semantic_selected, gold_claim)
    )
    semantic_precision = _safe_ratio(semantic_tp, semantic_tp + semantic_fp)
    semantic_recall = _safe_ratio(semantic_tp, semantic_tp + semantic_fn)

    classified_rows = [
        row
        for row in records
        if row["row_source"] == "AUTO_VALUE"
        and row["prediction"]["detected"]
        and row["automatic"].get("candidate_class")
    ]
    class_confusion = Counter(
        (
            row["gold"]["eligibility"],
            row["automatic"]["candidate_class"],
        )
        for row in classified_rows
    )
    class_exact = sum(
        gold_class == predicted_class
        for gold_class, predicted_class in class_confusion.elements()
    )
    class_kosis_tp = class_confusion[
        ("KOSIS_CANDIDATE", "KOSIS_CANDIDATE")
    ]
    class_kosis_predicted = sum(
        count
        for (gold_class, predicted_class), count in class_confusion.items()
        if predicted_class == "KOSIS_CANDIDATE"
    )
    class_kosis_gold = sum(
        count
        for (gold_class, predicted_class), count in class_confusion.items()
        if gold_class == "KOSIS_CANDIDATE"
    )
    class_kosis_precision = _safe_ratio(
        class_kosis_tp,
        class_kosis_predicted,
    )
    class_kosis_recall = _safe_ratio(class_kosis_tp, class_kosis_gold)

    predicted_pass = [row["automatic"]["action"] == "PASS" for row in records]
    gold_kosis = [
        row["gold"]["eligibility"] == "KOSIS_CANDIDATE" for row in records
    ]
    route_tp = sum(p and g for p, g in zip(predicted_pass, gold_kosis))
    route_fp = sum(p and not g for p, g in zip(predicted_pass, gold_kosis))
    route_fn = sum(not p and g for p, g in zip(predicted_pass, gold_kosis))
    route_tn = sum(not p and not g for p, g in zip(predicted_pass, gold_kosis))
    route_precision = _safe_ratio(route_tp, route_tp + route_fp)
    route_recall = _safe_ratio(route_tp, route_tp + route_fn)

    gold_rows = [row for row in records if row["gold"]["eligibility"] == "KOSIS_CANDIDATE"]
    passed_gold_rows = [
        row
        for row in gold_rows
        if row["automatic"]["action"] == "PASS"
    ]

    def field_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            **{
                field: _scalar_metric(rows, field)
                for field in ("indicator", "measurement_type", "period")
            },
            **{
                field: _role_metric(rows, field)
                for field in ("population", "item", "dimension")
            },
        }

    def six_field_exact(row: dict[str, Any]) -> bool:
        return all(
            normalize_exact(row["automatic"][field])
            == normalize_exact(row["gold"][field])
            for field in ("indicator", "measurement_type", "period")
        ) and all(
            {
                normalize_exact(value)
                for value in row["automatic"][field]
                if normalize_exact(value)
            }
            == {
                normalize_exact(value)
                for value in row["gold"][field]
                if normalize_exact(value)
            }
            for field in ("population", "item", "dimension")
        )

    six_exact_all = sum(six_field_exact(row) for row in gold_rows)
    six_exact_passed = sum(six_field_exact(row) for row in passed_gold_rows)
    abstained = [
        row for row in records if row["automatic"]["action"] != "PASS"
    ]
    abstention_correct = sum(
        row["gold"]["eligibility"] != "KOSIS_CANDIDATE" for row in abstained
    )
    per_article: dict[str, dict[str, int]] = {}
    for article_idx in sorted(
        {row["article_idx"] for row in records},
        key=int,
    ):
        article_rows = [
            row for row in records if row["article_idx"] == article_idx
        ]
        per_article[article_idx] = {
            "evaluation_units": len(article_rows),
            "gold_claims": sum(row["gold"]["claim"] for row in article_rows),
            "gold_kosis": sum(
                row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                for row in article_rows
            ),
            "detected_gold_claims": sum(
                row["row_source"] == "AUTO_VALUE" and row["gold"]["claim"]
                for row in article_rows
            ),
            "semantic_selected_gold_claims": sum(
                row["prediction"]["detected"] and row["gold"]["claim"]
                for row in article_rows
            ),
            "passed_gold_kosis": sum(
                row["automatic"]["action"] == "PASS"
                and row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                for row in article_rows
            ),
            "missed_gold_kosis": sum(
                not row["prediction"]["detected"]
                and row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                for row in article_rows
            ),
            "blocked_gold_kosis": sum(
                row["prediction"]["detected"]
                and row["automatic"]["action"] != "PASS"
                and row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                for row in article_rows
            ),
            "passed_non_kosis": sum(
                row["automatic"]["action"] == "PASS"
                and row["gold"]["eligibility"] != "KOSIS_CANDIDATE"
                for row in article_rows
            ),
        }

    return {
        "selection_scope": {
            "description": (
                "개발에 사용하지 않은 KOSIS seed 6기사의 전체 117문장과 "
                "131 value candidate를 검토하고, value regex 미검출 claim을 "
                "추가한 article-level holdout"
            ),
            "rows": len(records),
            "articles": len({row["article_idx"] for row in records}),
            "row_source_counts": dict(Counter(row["row_source"] for row in records)),
            "eligibility_counts": dict(Counter(
                row["gold"]["eligibility"] for row in records
            )),
            "automatic_action_counts": dict(Counter(
                row["automatic"]["action"] for row in records
            )),
            "per_article": per_article,
        },
        "claim_detection": {
            "prediction_definition": (
                "frozen regex value candidate가 존재하는 AUTO_VALUE 행"
            ),
            "gold_positive_definition": "claim 여부=YES (KOSIS or OUT_OF_SCOPE)",
            "true_positive": claim_tp,
            "false_positive": claim_fp,
            "false_negative": claim_fn,
            "true_negative": claim_tn,
            "precision": claim_precision,
            "recall": claim_recall,
            "f1": _f1(claim_precision, claim_recall),
            "false_positive_review_ids": [
                row["review_id"]
                for row, prediction, gold in zip(
                    records, candidate_detected, gold_claim
                )
                if prediction and not gold
            ],
            "false_negative_review_ids": [
                row["review_id"]
                for row, prediction, gold in zip(
                    records, candidate_detected, gold_claim
                )
                if not prediction and gold
            ],
        },
        "semantic_selection": {
            "prediction_definition": (
                "HCX skeleton이 target value candidate를 선택했거나, "
                "ADDED_MISSED claim을 문맥+target으로 복원한 행"
            ),
            "gold_positive_definition": "claim 여부=YES (KOSIS or OUT_OF_SCOPE)",
            "true_positive": semantic_tp,
            "false_positive": semantic_fp,
            "false_negative": semantic_fn,
            "true_negative": semantic_tn,
            "precision": semantic_precision,
            "recall": semantic_recall,
            "f1": _f1(semantic_precision, semantic_recall),
            "false_positive_review_ids": [
                row["review_id"]
                for row, prediction, gold in zip(
                    records, semantic_selected, gold_claim
                )
                if prediction and not gold
            ],
            "false_negative_review_ids": [
                row["review_id"]
                for row, prediction, gold in zip(
                    records, semantic_selected, gold_claim
                )
                if not prediction and gold
            ],
        },
        "candidate_classification": {
            "scope": "AUTO_VALUE rows with a candidate-first HCX record",
            "classified_rows": len(classified_rows),
            "exact_rows": class_exact,
            "exact_accuracy": _safe_ratio(
                class_exact,
                len(classified_rows),
            ),
            "confusion": {
                f"{gold_class}->{predicted_class}": count
                for (gold_class, predicted_class), count
                in sorted(class_confusion.items())
            },
            "kosis_precision": class_kosis_precision,
            "kosis_recall": class_kosis_recall,
            "kosis_f1": _f1(
                class_kosis_precision,
                class_kosis_recall,
            ),
        },
        "routing": {
            "gold_positive_definition": "KOSIS_CANDIDATE",
            "true_positive": route_tp,
            "false_positive": route_fp,
            "false_negative": route_fn,
            "true_negative": route_tn,
            "precision": route_precision,
            "recall": route_recall,
            "f1": _f1(route_precision, route_recall),
            "false_positive_review_ids": [
                row["review_id"]
                for row, prediction, gold in zip(
                    records, predicted_pass, gold_kosis
                )
                if prediction and not gold
            ],
            "false_negative_review_ids": [
                row["review_id"]
                for row, prediction, gold in zip(
                    records, predicted_pass, gold_kosis
                )
                if not prediction and gold
            ],
        },
        "abstention": {
            "rows": len(abstained),
            "correct_non_kosis_rows": abstention_correct,
            "routing_abstention_precision": _safe_ratio(
                abstention_correct, len(abstained)
            ),
            "kosis_rows_lost": sum(
                row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                for row in abstained
            ),
        },
        "field_metrics_gold_kosis_end_to_end": field_metrics(gold_rows),
        "field_metrics_gold_kosis_automatic_pass": field_metrics(passed_gold_rows),
        "complete_record": {
            "gold_kosis_rows": len(gold_rows),
            "automatic_pass_gold_rows": len(passed_gold_rows),
            "six_field_exact_end_to_end_rows": six_exact_all,
            "six_field_exact_end_to_end_accuracy": _safe_ratio(
                six_exact_all, len(gold_rows)
            ),
            "six_field_exact_given_pass_rows": six_exact_passed,
            "six_field_exact_given_pass_accuracy": _safe_ratio(
                six_exact_passed, len(passed_gold_rows)
            ),
            "definition": (
                "indicator, measurement, period, population, item, dimension의 "
                "정규화 exact가 모두 일치; 미검출·BLOCKED는 end-to-end 오답"
            ),
        },
        "errors": {
            "missed_kosis_by_source": dict(Counter(
                row["row_source"]
                for row in records
                if (
                    row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                    and not row["prediction"]["detected"]
                )
            )),
            "blocked_kosis_by_article": dict(Counter(
                row["article_idx"]
                for row in records
                if (
                    row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                    and row["prediction"]["detected"]
                    and row["automatic"]["action"] != "PASS"
                )
            )),
            "passed_non_kosis_by_eligibility": dict(Counter(
                row["gold"]["eligibility"]
                for row in records
                if (
                    row["gold"]["eligibility"] != "KOSIS_CANDIDATE"
                    and row["automatic"]["action"] == "PASS"
                )
            )),
            "blocked_kosis_semantic_errors": dict(Counter(
                error
                for row in records
                if (
                    row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                    and row["prediction"]["detected"]
                    and row["automatic"]["action"] != "PASS"
                )
                for error in row["automatic"]["semantic_errors"]
            )),
            "blocked_kosis_binding_errors": dict(Counter(
                error
                for row in records
                if (
                    row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                    and row["prediction"]["detected"]
                    and row["automatic"]["action"] != "PASS"
                )
                for error in row["automatic"]["binding_errors"]
            )),
            "blocked_kosis_scope_errors": dict(Counter(
                error
                for row in records
                if (
                    row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                    and row["prediction"]["detected"]
                    and row["automatic"]["action"] != "PASS"
                )
                for error in row["automatic"]["scope_errors"]
            )),
        },
    }


def evaluate_holdout(
    *,
    snapshot: dict[str, Any],
    fixture_root: Path,
    run_dir: Path,
    verify_workbook: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sentence_records, gold_records, integrity = parse_adjudication_snapshot(
        snapshot,
        fixture_root=fixture_root,
        verify_workbook=verify_workbook,
    )
    predictions, run_integrity = apply_run_predictions(
        gold_records,
        fixture_root=fixture_root,
        run_dir=run_dir,
    )
    return predictions, {
        "artifact_status": "CONFIRMED_HUMAN_ADJUDICATED_HOLDOUT",
        "source_workbook": integrity["workbook_path"],
        "source_workbook_sha256": integrity["workbook_sha256"],
        "fixture_root": str(fixture_root),
        "prediction_run": str(run_dir),
        "review_integrity": integrity,
        "sentence_review_rows": len(sentence_records),
        "run_integrity": run_integrity,
        "current_candidate_extractor": evaluate_current_candidate_extractor(
            gold_records,
            fixture_root=fixture_root,
        ),
        "evaluation": evaluate_records(predictions),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    predictions, report = evaluate_holdout(
        snapshot=_load_json(args.snapshot),
        fixture_root=args.fixture_root,
        run_dir=args.run_dir,
    )
    _write_jsonl(args.output, predictions)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "artifact_status": report["artifact_status"],
        "review_integrity": report["review_integrity"],
        "claim_detection": report["evaluation"]["claim_detection"],
        "routing": report["evaluation"]["routing"],
        "complete_record": report["evaluation"]["complete_record"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
