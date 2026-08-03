"""Apply human-confirmed semantic-role labels to the article HCX gold fixture.

The spreadsheet itself is first read with the spreadsheet artifact runtime and
exported as a JSON snapshot.  This module validates that snapshot against the
source candidate IDs, then writes a new gold artifact without mutating either
the original gold fixture or the reviewed workbook.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .article_hcx_gold_fixture import load_jsonl


ROLE_COLUMNS = {
    "population": 7,
    "item": 8,
    "dimension": 9,
}
EXPECTED_CANDIDATE_KIND = {
    "population": {"semantic_evidence"},
    "item": {"semantic_evidence"},
    # The generic dimension extractor currently covers only selected controlled
    # vocabularies. Human adjudication may therefore bind an exact-source
    # industry/sector/exclusion value through the semantic-evidence catalog.
    "dimension": {"dimension", "semantic_evidence"},
}
KOREAN_PARTICLE_SUFFIXES = (
    "에게서", "으로", "에서", "에게", "께서",
    "은", "는", "이", "가", "을", "를", "와", "과", "의", "에", "도", "만", "로",
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _split_role_cell(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty adjudication cell; use '없음' for an empty role")
    if text == "없음":
        return []
    values = [part.strip() for part in text.split(",")]
    if not all(values):
        raise ValueError(f"invalid comma-separated adjudication value: {text!r}")
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate adjudication value: {text!r}")
    return values


def _candidate_record(row: list[Any]) -> dict[str, Any]:
    return {
        "fixture_id": str(row[0] or ""),
        "article_idx": str(row[1] or ""),
        "value_sentence_id": row[2],
        "indicator_norm": str(row[3] or ""),
        "value_text": str(row[4] or ""),
        "kind": str(row[5] or ""),
        "dimension_type": str(row[6] or ""),
        "text": str(row[7] or ""),
        "candidate_id": str(row[8] or ""),
        "sentence_id": row[9],
        "selection_note": str(row[10] or ""),
        "run": str(row[11] or ""),
    }


def _resolve_role_values(
    *,
    fixture_id: str,
    role: str,
    texts: list[str],
    value_sentence_id: object,
    candidates_by_fixture: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    expected_kinds = EXPECTED_CANDIDATE_KIND[role]
    for text in texts:
        fixture_candidates = [
            candidate
            for candidate in candidates_by_fixture.get(fixture_id, [])
            if candidate["kind"] in expected_kinds
            and candidate["sentence_id"] == value_sentence_id
        ]
        matches = [
            candidate
            for candidate in fixture_candidates
            if candidate["text"] == text
        ]
        resolution_mode = "EXACT"
        if not matches:
            matches = [
                candidate
                for candidate in fixture_candidates
                if candidate["text"].startswith(text)
                and candidate["text"][len(text):] in KOREAN_PARTICLE_SUFFIXES
            ]
            resolution_mode = "PARTICLE_VARIANT"
        if not matches and len(text.split()) > 1:
            composite_matches: list[dict[str, Any]] = []
            for part in text.split():
                part_matches = [
                    candidate for candidate in fixture_candidates
                    if candidate["text"] == part
                ]
                if role == "dimension" and any(
                    candidate["kind"] == "dimension" for candidate in part_matches
                ):
                    part_matches = [
                        candidate for candidate in part_matches
                        if candidate["kind"] == "dimension"
                    ]
                unique_part_matches = {
                    candidate["candidate_id"]: candidate
                    for candidate in part_matches
                }
                if len(unique_part_matches) != 1:
                    composite_matches = []
                    break
                composite_matches.append(next(iter(unique_part_matches.values())))
            if composite_matches:
                matches = composite_matches
                resolution_mode = "COMPOSITE"
        if role == "dimension" and any(
            candidate["kind"] == "dimension" for candidate in matches
        ):
            matches = [
                candidate for candidate in matches
                if candidate["kind"] == "dimension"
            ]
        unique = {candidate["candidate_id"]: candidate for candidate in matches}
        if resolution_mode != "COMPOSITE" and len(unique) != 1:
            raise ValueError(
                f"{fixture_id} {role}={text!r}: expected one exact source candidate "
                f"in sentence {value_sentence_id}, found {sorted(unique)}"
            )
        if resolution_mode == "COMPOSITE" and len(unique) != len(text.split()):
            raise ValueError(
                f"{fixture_id} {role}={text!r}: composite candidate resolution is ambiguous"
            )
        for candidate in unique.values():
            resolved.append({
                **candidate,
                "adjudicated_text": text,
                "resolution_mode": resolution_mode,
            })
    return resolved


def apply_adjudications(
    scaffold_rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    workbook_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    review_rows = snapshot.get("review_rows")
    candidate_rows = snapshot.get("candidate_rows")
    if not isinstance(review_rows, list) or not isinstance(candidate_rows, list):
        raise ValueError("snapshot must contain review_rows and candidate_rows arrays")

    candidates_by_fixture: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_candidate in candidate_rows:
        candidate = _candidate_record(raw_candidate)
        candidates_by_fixture[candidate["fixture_id"]].append(candidate)
    scaffold_fallback_candidates = 0
    for scaffold_row in scaffold_rows:
        fixture_id = str(scaffold_row.get("fixture_id") or "")
        dimension_texts = scaffold_row.get("dimension_texts")
        dimension_texts = (
            list(dimension_texts) if isinstance(dimension_texts, list) else []
        )
        labeling = scaffold_row.get("semantic_role_labeling")
        labeling = labeling if isinstance(labeling, dict) else {}
        dimension_ids = list(labeling.get("dimension_candidate_ids_draft") or [])
        if len(dimension_texts) != len(dimension_ids):
            continue
        existing_ids = {
            candidate["candidate_id"]
            for candidate in candidates_by_fixture.get(fixture_id, [])
        }
        for text, candidate_id in zip(dimension_texts, dimension_ids):
            if candidate_id in existing_ids:
                continue
            candidates_by_fixture[fixture_id].append({
                "fixture_id": fixture_id,
                "article_idx": str(scaffold_row.get("article_idx") or ""),
                "value_sentence_id": scaffold_row.get("value_sentence_id"),
                "indicator_norm": str(scaffold_row.get("indicator_norm") or ""),
                "value_text": str(scaffold_row.get("value_text") or ""),
                "kind": "dimension",
                "dimension_type": "",
                "text": str(text),
                "candidate_id": str(candidate_id),
                "sentence_id": scaffold_row.get("value_sentence_id"),
                "selection_note": "scaffold dimension draft",
                "run": "scaffold_full_catalog_fallback",
            })
            scaffold_fallback_candidates += 1

    review_by_fixture: dict[str, list[Any]] = {}
    for review in review_rows:
        fixture_id = str(review[0] or "")
        if not fixture_id:
            raise ValueError("review row without fixture_id")
        if fixture_id in review_by_fixture:
            raise ValueError(f"duplicate review row: {fixture_id}")
        review_by_fixture[fixture_id] = review

    eligible = [
        row for row in scaffold_rows if row.get("eligibility") == "KOSIS_CANDIDATE"
    ]
    expected_fixtures = {str(row.get("fixture_id") or "") for row in eligible}
    reviewed_fixtures = set(review_by_fixture)
    if expected_fixtures != reviewed_fixtures:
        raise ValueError(
            "review/scaffold fixture mismatch: "
            f"missing={sorted(expected_fixtures - reviewed_fixtures)}, "
            f"unexpected={sorted(reviewed_fixtures - expected_fixtures)}"
        )

    resolved_by_fixture: dict[str, dict[str, Any]] = {}
    for fixture_id in sorted(review_by_fixture):
        review = review_by_fixture[fixture_id]
        if str(review[10] or "").strip() != "확정":
            raise ValueError(f"{fixture_id}: review status is not 확정")
        if str(review[11] or "").strip() != "완료":
            raise ValueError(f"{fixture_id}: QC status is not 완료")
        sentence_text = str(review[3] or "")
        value_sentence_id = review[2]
        resolved_roles: dict[str, list[dict[str, Any]]] = {}
        role_texts: dict[str, list[str]] = {}
        for role, column in ROLE_COLUMNS.items():
            texts = _split_role_cell(review[column])
            for text in texts:
                if text not in sentence_text:
                    raise ValueError(
                        f"{fixture_id} {role}={text!r}: not found in reviewed source sentence"
                    )
            role_texts[role] = texts
            resolved_roles[role] = _resolve_role_values(
                fixture_id=fixture_id,
                role=role,
                texts=texts,
                value_sentence_id=value_sentence_id,
                candidates_by_fixture=candidates_by_fixture,
            )
        resolved_by_fixture[fixture_id] = {
            "review": review,
            "role_texts": role_texts,
            "resolved_roles": resolved_roles,
        }

    output_rows: list[dict[str, Any]] = []
    resolution_rows: list[dict[str, Any]] = []
    changed_dimension_rows = 0
    for source_row in scaffold_rows:
        fixture_id = str(source_row.get("fixture_id") or "")
        if fixture_id not in resolved_by_fixture:
            output_rows.append(source_row)
            continue
        resolved = resolved_by_fixture[fixture_id]
        review = resolved["review"]
        role_texts = resolved["role_texts"]
        resolved_roles = resolved["resolved_roles"]
        original_dimensions = source_row.get("dimension_texts")
        original_dimensions = (
            list(original_dimensions) if isinstance(original_dimensions, list) else []
        )
        if original_dimensions != role_texts["dimension"]:
            changed_dimension_rows += 1
        draft = source_row.get("semantic_role_labeling")
        draft = dict(draft) if isinstance(draft, dict) else {}
        draft_field_status = draft.get("field_status")
        draft_field_status = (
            dict(draft_field_status) if isinstance(draft_field_status, dict) else {}
        )
        candidate_ids = {
            role: [candidate["candidate_id"] for candidate in resolved_roles[role]]
            for role in ROLE_COLUMNS
        }
        candidate_kinds = {
            role: [candidate["kind"] for candidate in resolved_roles[role]]
            for role in ROLE_COLUMNS
        }
        resolution_details = {
            role: [
                {
                    "adjudicated_text": candidate["adjudicated_text"],
                    "candidate_text": candidate["text"],
                    "candidate_id": candidate["candidate_id"],
                    "candidate_kind": candidate["kind"],
                    "resolution_mode": candidate["resolution_mode"],
                }
                for candidate in resolved_roles[role]
            ]
            for role in ROLE_COLUMNS
        }
        semantic_role_gold = {
            "adjudication_status": "CONFIRMED",
            "target_value_candidate_ids": list(
                draft.get("target_value_candidate_ids_draft") or []
            ),
            "indicator_evidence_candidate_ids": list(
                draft.get("indicator_evidence_candidate_ids_draft") or []
            ),
            "population_evidence_candidate_ids": candidate_ids["population"],
            "item_evidence_candidate_ids": candidate_ids["item"],
            "dimension_candidate_ids": candidate_ids["dimension"],
            "dimension_candidate_kinds": candidate_kinds["dimension"],
            "candidate_resolution": resolution_details,
            "population_texts": role_texts["population"],
            "item_texts": role_texts["item"],
            "dimension_texts": role_texts["dimension"],
            "review_status": "확정",
            "qc_status": "완료",
            "review_note": str(review[12] or "").strip(),
            "source_workbook": Path(str(snapshot.get("workbook_path") or "")).name,
            "source_workbook_sha256": workbook_sha256,
        }
        confirmed_labeling = {
            **draft,
            **semantic_role_gold,
            "field_status": {
                **draft_field_status,
                "population": (
                    "CONFIRMED_EXACT_SOURCE"
                    if role_texts["population"] else "CONFIRMED_EMPTY"
                ),
                "item": (
                    "CONFIRMED_EXACT_SOURCE"
                    if role_texts["item"] else "CONFIRMED_EMPTY"
                ),
                "dimension": (
                    "CONFIRMED_EXACT_SOURCE"
                    if role_texts["dimension"] else "CONFIRMED_EMPTY"
                ),
            },
        }
        output_rows.append({
            **source_row,
            "pre_adjudication_dimension_texts": original_dimensions,
            "population_texts": role_texts["population"],
            "item_texts": role_texts["item"],
            "dimension_texts": role_texts["dimension"],
            "semantic_role_labeling": confirmed_labeling,
            "semantic_role_gold": semantic_role_gold,
        })
        resolution_rows.append({
            "fixture_id": fixture_id,
            "article_idx": str(source_row.get("article_idx") or ""),
            "value_text": str(source_row.get("value_text") or ""),
            "population_texts": role_texts["population"],
            "item_texts": role_texts["item"],
            "dimension_texts": role_texts["dimension"],
            "population_candidate_ids": candidate_ids["population"],
            "item_candidate_ids": candidate_ids["item"],
            "dimension_candidate_ids": candidate_ids["dimension"],
            "dimension_candidate_kinds": candidate_kinds["dimension"],
            "candidate_resolution": resolution_details,
            "review_note": semantic_role_gold["review_note"],
        })

    role_nonempty_rows = {
        role: sum(bool(row[f"{role}_texts"]) for row in resolution_rows)
        for role in ROLE_COLUMNS
    }
    role_label_counts = {
        role: sum(len(row[f"{role}_texts"]) for row in resolution_rows)
        for role in ROLE_COLUMNS
    }
    report = {
        "artifact_status": "CONFIRMED_HUMAN_ADJUDICATED",
        "source_workbook": snapshot.get("workbook_path"),
        "source_workbook_sha256": workbook_sha256,
        "input_scaffold_rows": len(scaffold_rows),
        "eligible_rows": len(eligible),
        "adjudicated_rows": len(resolution_rows),
        "review_status_counts": dict(
            Counter(str(row[10] or "").strip() for row in review_rows)
        ),
        "qc_status_counts": dict(
            Counter(str(row[11] or "").strip() for row in review_rows)
        ),
        "candidate_rows": len(candidate_rows),
        "scaffold_fallback_candidates": scaffold_fallback_candidates,
        "candidate_resolution_errors": 0,
        "candidate_resolution_mode_counts": dict(Counter(
            detail["resolution_mode"]
            for row in resolution_rows
            for role in ROLE_COLUMNS
            for detail in row["candidate_resolution"][role]
        )),
        "role_nonempty_rows": role_nonempty_rows,
        "role_label_counts": role_label_counts,
        "changed_dimension_rows": changed_dimension_rows,
        "rows": resolution_rows,
    }
    return output_rows, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scaffold", type=Path, required=True)
    parser.add_argument("--review-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    snapshot = _read_json(args.review_snapshot)
    workbook_path = Path(str(snapshot.get("workbook_path") or ""))
    workbook_sha256 = (
        hashlib.sha256(workbook_path.read_bytes()).hexdigest()
        if workbook_path.is_file() else None
    )
    rows, report = apply_adjudications(
        load_jsonl(args.scaffold),
        snapshot,
        workbook_sha256=workbook_sha256,
    )
    _write_jsonl(args.output, rows)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        key: value for key, value in report.items() if key != "rows"
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
