"""Validate and apply the human review of the HCX blind-expansion sample.

The reviewed workbook is read by the spreadsheet artifact runtime and saved as
an immutable JSON snapshot.  This module validates the snapshot against the
r13 run, emits a text-normalized gold JSONL, and computes field/routing metrics.
It intentionally does not parse XLSX files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


REVIEW_HEADERS = [
    "검토ID",
    "기사ID",
    "기사제목",
    "claim#",
    "원문 근거문장",
    "목표값",
    "자동 indicator",
    "자동 measurement",
    "자동 period",
    "자동 population",
    "자동 item",
    "자동 dimension",
    "semantic",
    "binding",
    "scope",
    "자동조치",
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

ALLOWED = {
    "automatic_action": {"PASS", "BLOCKED"},
    "eligibility": {
        "KOSIS_CANDIDATE",
        "OUT_OF_SCOPE",
        "NOT_CLAIM",
        "AMBIGUOUS",
    },
    "measurement_type": {
        "INDEX_LEVEL",
        "LEVEL",
        "CHANGE_RATE",
        "CHANGE_POINT",
    },
    "value_pairing": {"YES", "NO", "AMBIGUOUS"},
    "final_status": {"CONFIRMED", "CORRECTED", "REJECTED"},
}


def _read_json(path: Path) -> dict[str, Any]:
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


def _text(value: object) -> str:
    return str(value or "").strip()


def _explicit_empty(value: object) -> str:
    text = _text(value)
    return "" if text == "없음" else text


def _split_role(value: object, *, delimiter: str) -> list[str]:
    text = _explicit_empty(value)
    if not text:
        return []
    values = [part.strip() for part in text.split(delimiter)]
    if not all(values):
        raise ValueError(f"invalid role list: {value!r}")
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate role value: {value!r}")
    return values


def normalize_exact(value: object) -> str:
    text = unicodedata.normalize("NFKC", _explicit_empty(value)).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    return (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )


def _scalar_metric(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    matches = [
        normalize_exact(row["automatic"][field])
        == normalize_exact(row["gold"][field])
        for row in records
    ]
    auto_missing_gold_present = sum(
        not normalize_exact(row["automatic"][field])
        and bool(normalize_exact(row["gold"][field]))
        for row in records
    )
    auto_present_gold_empty = sum(
        bool(normalize_exact(row["automatic"][field]))
        and not normalize_exact(row["gold"][field])
        for row in records
    )
    return {
        "correct": sum(matches),
        "total": len(matches),
        "accuracy": _safe_ratio(sum(matches), len(matches)),
        "auto_missing_gold_present": auto_missing_gold_present,
        "auto_present_gold_empty": auto_present_gold_empty,
        "mismatch_review_ids": [
            row["review_id"] for row, match in zip(records, matches) if not match
        ],
    }


def _role_metric(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    true_positive = 0
    predicted = 0
    gold = 0
    exact_rows = 0
    auto_missing_gold_present = 0
    auto_present_gold_empty = 0
    mismatch_ids: list[str] = []
    for row in records:
        auto_set = {
            normalize_exact(value)
            for value in row["automatic"][field]
            if normalize_exact(value)
        }
        gold_set = {
            normalize_exact(value)
            for value in row["gold"][field]
            if normalize_exact(value)
        }
        true_positive += len(auto_set & gold_set)
        predicted += len(auto_set)
        gold += len(gold_set)
        if auto_set == gold_set:
            exact_rows += 1
        else:
            mismatch_ids.append(row["review_id"])
        if not auto_set and gold_set:
            auto_missing_gold_present += 1
        if auto_set and not gold_set:
            auto_present_gold_empty += 1
    precision = _safe_ratio(true_positive, predicted)
    recall = _safe_ratio(true_positive, gold)
    return {
        "row_exact": exact_rows,
        "total_rows": len(records),
        "row_exact_accuracy": _safe_ratio(exact_rows, len(records)),
        "label_true_positive": true_positive,
        "label_predicted": predicted,
        "label_gold": gold,
        "label_precision": precision,
        "label_recall": recall,
        "label_f1": _f1(precision, recall),
        "auto_missing_gold_present": auto_missing_gold_present,
        "auto_present_gold_empty": auto_present_gold_empty,
        "mismatch_review_ids": mismatch_ids,
    }


def parse_review_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    matrix = snapshot.get("review_matrix")
    if not isinstance(matrix, list) or len(matrix) < 5:
        raise ValueError("review_matrix is missing or too short")
    headers = matrix[3]
    if headers != REVIEW_HEADERS:
        raise ValueError(f"unexpected review headers: {headers!r}")
    raw_rows = [row for row in matrix[4:] if any(cell not in (None, "") for cell in row)]
    if len(raw_rows) != 40:
        raise ValueError(f"expected 40 reviewed rows, found {len(raw_rows)}")

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sheet_row, row in enumerate(raw_rows, start=5):
        if len(row) != len(REVIEW_HEADERS):
            raise ValueError(f"sheet row {sheet_row}: expected 26 columns")
        review_id = _text(row[0])
        if not review_id:
            raise ValueError(f"sheet row {sheet_row}: missing review_id")
        if review_id in seen:
            raise ValueError(f"duplicate review_id: {review_id}")
        seen.add(review_id)
        article_idx = _text(row[1])
        claim_index = int(row[3])
        expected_id = f"{article_idx}-{claim_index + 1:03d}"
        if review_id != expected_id:
            raise ValueError(
                f"{review_id}: expected review_id {expected_id} from article/claim"
            )
        for column in range(16, 26):
            if row[column] in (None, ""):
                raise ValueError(
                    f"{review_id}: empty required review cell {REVIEW_HEADERS[column]}"
                )
        automatic_action = _text(row[15])
        eligibility = _text(row[16])
        measurement_type = _text(row[18])
        value_pairing = _text(row[23])
        final_status = _text(row[24])
        for field, value in (
            ("automatic_action", automatic_action),
            ("eligibility", eligibility),
            ("measurement_type", measurement_type),
            ("value_pairing", value_pairing),
            ("final_status", final_status),
        ):
            if value not in ALLOWED[field]:
                raise ValueError(f"{review_id}: invalid {field}={value!r}")
        if final_status == "REJECTED" and eligibility == "KOSIS_CANDIDATE":
            raise ValueError(
                f"{review_id}: REJECTED row cannot remain KOSIS_CANDIDATE"
            )
        if eligibility == "KOSIS_CANDIDATE" and not _text(row[17]):
            raise ValueError(f"{review_id}: KOSIS candidate requires indicator gold")
        records.append({
            "review_id": review_id,
            "article_idx": article_idx,
            "article_title": _text(row[2]),
            "claim_index": claim_index,
            "source_sentence": _text(row[4]),
            "target_value": _text(row[5]),
            "automatic": {
                "indicator": _text(row[6]),
                "measurement_type": _text(row[7]),
                "period": _text(row[8]),
                "population": _split_role(row[9], delimiter="|"),
                "item": _split_role(row[10], delimiter="|"),
                "dimension": _split_role(row[11], delimiter="|"),
                "semantic_status": _text(row[12]),
                "binding_status": _text(row[13]),
                "scope_status": _text(row[14]),
                "action": automatic_action,
            },
            "gold": {
                "eligibility": eligibility,
                "indicator": _text(row[17]),
                "measurement_type": measurement_type,
                "period": _explicit_empty(row[19]),
                "population": _split_role(row[20], delimiter=","),
                "item": _split_role(row[21], delimiter=","),
                "dimension": _split_role(row[22], delimiter=","),
                "value_pairing": value_pairing,
                "final_status": final_status,
                "review_note": _text(row[25]),
            },
            "source": {
                "sheet": "검토",
                "row": sheet_row,
            },
        })
    return records


def validate_against_run(
    records: list[dict[str, Any]],
    run_dir: Path,
) -> dict[str, Any]:
    inputs = {
        _text(row.get("article_idx")): row
        for row in _load_jsonl(run_dir / "input.jsonl")
    }
    scopes = {
        (_text(row.get("article_idx")), int(row["claim_index"])): row
        for row in _load_jsonl(run_dir / "scope_validation.jsonl")
    }
    semantics = {
        (_text(row.get("article_idx")), int(row["claim_index"])): row
        for row in _load_jsonl(run_dir / "semantic_validation.jsonl")
    }
    candidates = {
        (_text(row.get("article_idx")), int(row["claim_index"])): row
        for row in _load_jsonl(run_dir / "span_candidates.jsonl")
    }
    bindings: dict[tuple[str, int], dict[str, Any]] = {}
    for article in _load_jsonl(run_dir / "validation.jsonl"):
        article_idx = _text(article.get("article_idx"))
        for claim in article.get("validation", {}).get("claims", []):
            bindings[(article_idx, int(claim["claim_index"]))] = claim

    errors: list[str] = []
    grounded_targets = 0
    empty_targets = 0
    for record in records:
        key = (record["article_idx"], record["claim_index"])
        article = inputs.get(record["article_idx"])
        if article is None:
            errors.append(f"{record['review_id']}: article missing from run input")
            continue
        if record["article_title"] != _text(article.get("title")):
            errors.append(f"{record['review_id']}: title mismatch")
        if record["source_sentence"] not in _text(article.get("article_text")):
            errors.append(f"{record['review_id']}: source sentence not in article")
        if key not in scopes or key not in semantics or key not in bindings:
            errors.append(f"{record['review_id']}: claim missing from r13 artifacts")
            continue
        scope_status = _text(scopes[key].get("scope_validation", {}).get("claim_status"))
        semantic_status = _text(
            semantics[key].get("semantic_validation", {}).get("status")
        )
        binding_status = _text(
            bindings.get(key, {}).get("validation", {}).get("claim_status")
        )
        expected_action = "PASS" if scope_status == "PASS" else "BLOCKED"
        for field, actual, expected in (
            ("action", record["automatic"]["action"], expected_action),
            ("semantic", record["automatic"]["semantic_status"], semantic_status),
            ("binding", record["automatic"]["binding_status"], binding_status),
            ("scope", record["automatic"]["scope_status"], scope_status),
        ):
            if actual != expected:
                errors.append(
                    f"{record['review_id']}: {field} mismatch "
                    f"(workbook={actual!r}, run={expected!r})"
                )
        value_texts = {
            _text(candidate.get("text"))
            for candidate in candidates.get(key, {}).get("candidates", [])
            if candidate.get("kind") == "value_unit"
        }
        if record["target_value"]:
            if record["target_value"] not in value_texts:
                errors.append(
                    f"{record['review_id']}: target is not an r13 value candidate"
                )
            else:
                grounded_targets += 1
        else:
            empty_targets += 1
            if record["gold"]["value_pairing"] == "YES":
                errors.append(
                    f"{record['review_id']}: empty target cannot have YES pairing"
                )
    if errors:
        raise ValueError("run integrity validation failed:\n" + "\n".join(errors))
    return {
        "status": "PASS",
        "review_rows": len(records),
        "unique_articles": len({row["article_idx"] for row in records}),
        "grounded_target_rows": grounded_targets,
        "empty_target_rows": empty_targets,
        "integrity_errors": 0,
    }


def evaluate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    scalar_metrics_all = {
        field: _scalar_metric(records, field)
        for field in ("indicator", "measurement_type", "period")
    }
    role_metrics_all = {
        field: _role_metric(records, field)
        for field in ("population", "item", "dimension")
    }
    predicted_pass = [
        row["automatic"]["action"] == "PASS" for row in records
    ]
    gold_routable = [
        row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
        and row["gold"]["value_pairing"] == "YES"
        for row in records
    ]
    true_positive = sum(p and g for p, g in zip(predicted_pass, gold_routable))
    false_positive = sum(p and not g for p, g in zip(predicted_pass, gold_routable))
    false_negative = sum(not p and g for p, g in zip(predicted_pass, gold_routable))
    true_negative = sum(not p and not g for p, g in zip(predicted_pass, gold_routable))
    precision = _safe_ratio(true_positive, true_positive + false_positive)
    recall = _safe_ratio(true_positive, true_positive + false_negative)

    exact_by_id: dict[str, bool] = {}
    for row in records:
        scalars_exact = all(
            normalize_exact(row["automatic"][field])
            == normalize_exact(row["gold"][field])
            for field in ("indicator", "measurement_type", "period")
        )
        roles_exact = all(
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
        exact_by_id[row["review_id"]] = scalars_exact and roles_exact
    routable_rows = [
        row for row, routable in zip(records, gold_routable) if routable
    ]
    scalar_metrics_routable = {
        field: _scalar_metric(routable_rows, field)
        for field in ("indicator", "measurement_type", "period")
    }
    role_metrics_routable = {
        field: _role_metric(routable_rows, field)
        for field in ("population", "item", "dimension")
    }
    complete_exact = sum(exact_by_id[row["review_id"]] for row in routable_rows)
    pass_rows = [
        row for row, predicted in zip(records, predicted_pass) if predicted
    ]
    blocked_rows = [
        row for row, predicted in zip(records, predicted_pass) if not predicted
    ]
    complete_exact_pass = sum(
        exact_by_id[row["review_id"]]
        and row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
        and row["gold"]["value_pairing"] == "YES"
        for row in pass_rows
    )

    return {
        "selection_scope": {
            "description": (
                "r13 자동 산출물에서 선택한 40행만 평가한다. 기사 전체의 "
                "미검출 claim은 라벨링하지 않아 claim-detection recall은 산출 불가하다."
            ),
            "rows": len(records),
            "articles": len({row["article_idx"] for row in records}),
            "automatic_action_counts": dict(
                Counter(row["automatic"]["action"] for row in records)
            ),
            "eligibility_counts": dict(
                Counter(row["gold"]["eligibility"] for row in records)
            ),
            "pairing_counts": dict(
                Counter(row["gold"]["value_pairing"] for row in records)
            ),
            "final_status_counts": dict(
                Counter(row["gold"]["final_status"] for row in records)
            ),
        },
        "routing": {
            "gold_positive_definition": "KOSIS_CANDIDATE and value_pairing=YES",
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "out_of_scope_pass_review_ids": [
                row["review_id"]
                for row, predicted, routable in zip(
                    records, predicted_pass, gold_routable
                )
                if predicted and not routable
            ],
            "blocked_but_routable_review_ids": [
                row["review_id"]
                for row, predicted, routable in zip(
                    records, predicted_pass, gold_routable
                )
                if not predicted and routable
            ],
        },
        "abstention": {
            "blocked_rows": len(blocked_rows),
            "blocked_requiring_human_intervention": sum(
                row["gold"]["final_status"] != "CONFIRMED"
                or row["gold"]["value_pairing"] != "YES"
                for row in blocked_rows
            ),
            "safety_abstention_precision": _safe_ratio(
                sum(
                    row["gold"]["final_status"] != "CONFIRMED"
                    or row["gold"]["value_pairing"] != "YES"
                    for row in blocked_rows
                ),
                len(blocked_rows),
            ),
            "blocked_unroutable_rows": sum(
                row["gold"]["eligibility"] != "KOSIS_CANDIDATE"
                or row["gold"]["value_pairing"] != "YES"
                for row in blocked_rows
            ),
            "routing_abstention_precision": _safe_ratio(
                sum(
                    row["gold"]["eligibility"] != "KOSIS_CANDIDATE"
                    or row["gold"]["value_pairing"] != "YES"
                    for row in blocked_rows
                ),
                len(blocked_rows),
            ),
            "blocked_routable_after_correction": sum(
                row["gold"]["eligibility"] == "KOSIS_CANDIDATE"
                and row["gold"]["value_pairing"] == "YES"
                for row in blocked_rows
            ),
            "note": (
                "safety는 자동 레코드를 그대로 통과시켜도 되는지, routing은 사람 "
                "교정 후 KOSIS 후보로 보낼 수 있는지를 각각 측정한다."
            ),
        },
        "field_metrics_all_selected": {
            **scalar_metrics_all,
            **role_metrics_all,
        },
        "field_metrics_gold_routable": {
            **scalar_metrics_routable,
            **role_metrics_routable,
        },
        "complete_record": {
            "gold_routable_rows": len(routable_rows),
            "six_field_exact_rows": complete_exact,
            "six_field_exact_accuracy": _safe_ratio(
                complete_exact, len(routable_rows)
            ),
            "automatic_pass_rows": len(pass_rows),
            "automatic_pass_six_field_exact_rows": complete_exact_pass,
            "automatic_pass_strict_precision": _safe_ratio(
                complete_exact_pass, len(pass_rows)
            ),
            "definition": (
                "indicator, measurement, period, population, item, dimension의 "
                "정규화 exact가 모두 일치"
            ),
        },
        "claim_detection_recall": {
            "status": "NOT_MEASURABLE",
            "reason": "선정 표본이 자동 산출 claim에 한정되어 미검출 claim gold가 없음",
        },
    }


def apply_review(
    snapshot: dict[str, Any],
    *,
    run_dir: Path,
    workbook_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = parse_review_snapshot(snapshot)
    integrity = validate_against_run(records, run_dir)
    snapshot_sha256 = _text(snapshot.get("workbook_sha256"))
    if workbook_sha256 and snapshot_sha256 != workbook_sha256:
        raise ValueError(
            "review workbook hash mismatch: "
            f"snapshot={snapshot_sha256}, actual={workbook_sha256}"
        )
    workbook_path = _text(snapshot.get("workbook_path"))
    for record in records:
        record["source"].update({
            "workbook_path": workbook_path,
            "workbook_sha256": snapshot_sha256,
            "run_dir": str(run_dir),
        })
    report = {
        "artifact_status": "CONFIRMED_HUMAN_ADJUDICATED",
        "source_workbook": workbook_path,
        "source_workbook_sha256": snapshot_sha256,
        "source_run": str(run_dir),
        "run_integrity": integrity,
        "evaluation": evaluate_records(records),
    }
    return records, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-snapshot", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    snapshot = _read_json(args.review_snapshot)
    workbook_path = Path(_text(snapshot.get("workbook_path")))
    workbook_sha256 = (
        hashlib.sha256(workbook_path.read_bytes()).hexdigest()
        if workbook_path.is_file()
        else None
    )
    rows, report = apply_review(
        snapshot,
        run_dir=args.run_dir,
        workbook_sha256=workbook_sha256,
    )
    _write_jsonl(args.output, rows)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "artifact_status": report["artifact_status"],
        "review_rows": report["run_integrity"]["review_rows"],
        "routing": report["evaluation"]["routing"],
        "complete_record": report["evaluation"]["complete_record"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
