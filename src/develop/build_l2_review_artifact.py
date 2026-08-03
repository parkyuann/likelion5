"""Build the L2 human-review contract without promoting suggestions to gold."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..claim_normalizer import normalize_time_ref


SOURCE_REGION_SUBTYPES = (
    "공식집계",
    "민간조사",
    "정책목표",
    "잠정추산",
    "법정기준",
)
SCOPE_ATTRIBUTION_TYPES = ("이 문장에서 도입", "앞에서 상속")
LABEL_PROVENANCE_VALUES = ("UNREVIEWED", "AUTO_DERIVED", "HUMAN_CONFIRMED")
DOMINANCE_VALUES = ("REGION_ID", "지배 없음")
ID_UNIQUENESS_SCOPE = "article"
SCOPE_ID_FORMAT = "{article_idx}-SC{nn}"
REGION_ID_FORMAT = "{article_idx}-R{nn}"
OUT_OF_SCOPE_REVIEW_REGIONS = {
    "OUT_OF_SCOPE_UNSPECIFIED",
    "MIXED",
    "NO_VERIFIABLE_NUMERIC_CLAIM",
}


def _matrix_records(
    snapshot: dict[str, Any],
    name: str,
) -> list[dict[str, Any]]:
    matrix = snapshot.get("matrices", {}).get(name, [])
    normalized = [
        row.get("value") if isinstance(row, dict) else row
        for row in matrix
    ]
    normalized = [row for row in normalized if isinstance(row, list)]
    if not normalized:
        raise ValueError(f"snapshot matrix is empty: {name}")
    header = [str(value) for value in normalized[0]]
    return [
        dict(zip(header, [*row, *([None] * (len(header) - len(row)))]))
        for row in normalized[1:]
    ]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _value_candidate_span_ids(
    sentence: dict[str, Any],
    claims: list[dict[str, Any]],
) -> str:
    values = [
        value.strip()
        for value in str(sentence.get("자동 value candidates") or "").split("|")
        if value.strip()
    ]
    remaining = [
        (
            str(claim.get("target value") or "").strip(),
            str(claim.get("candidate span ID") or "").strip(),
        )
        for claim in claims
    ]
    rendered = []
    for value in values:
        match_index = next(
            (
                index
                for index, (target, span_id) in enumerate(remaining)
                if target == value and span_id
            ),
            None,
        )
        if match_index is None:
            raise ValueError(
                "missing deterministic candidate span ID for "
                f"{sentence.get('문장검토ID')}: {value}"
            )
        _, span_id = remaining.pop(match_index)
        rendered.append(f"{value}={span_id}")
    return " | ".join(rendered)


def _automatic_context_ids(
    article_idx: str,
    sentence_id: int,
) -> tuple[str, str]:
    sequence = sentence_id + 1
    return (
        f"{article_idx}-SC{sequence:02d}",
        f"{article_idx}-R{sequence:02d}",
    )


def _span_suggestion(text: str, indicator: str) -> dict[str, Any] | None:
    if not indicator or indicator == "없음":
        return None
    exact_start = text.find(indicator)
    if exact_start >= 0:
        return {
            "text": indicator,
            "char_start": exact_start,
            "char_end": exact_start + len(indicator),
            "match_method": "EXACT",
        }
    terms = [
        token for token in re.findall(r"[가-힣A-Za-z0-9·]+", indicator)
        if len(token) >= 2 and token not in {"전체", "변화", "증감률"}
    ]
    matches = []
    for term in terms:
        start = text.find(term)
        if start >= 0:
            matches.append((len(term), start, term))
    if not matches:
        return None
    _, start, term = max(matches)
    return {
        "text": term,
        "char_start": start,
        "char_end": start + len(term),
        "match_method": "PARTIAL_TOKEN",
    }


def _source_subtype_suggestion(claim: dict[str, Any]) -> str | None:
    eligibility = str(claim.get("검증대상 gold") or "")
    if eligibility == "KOSIS_CANDIDATE":
        return "공식집계"
    if eligibility != "OUT_OF_SCOPE":
        return None
    note = " ".join(
        str(claim.get(field) or "")
        for field in ("검토메모", "indicator gold", "원문 문장")
    )
    if re.search(r"(?:민간|협회|CEO|연구소|설문)", note):
        return "민간조사"
    if re.search(r"(?:목표|계획|예정|추진)", note):
        return "정책목표"
    if re.search(r"(?:잠정|추정|추산|임시)", note):
        return "잠정추산"
    if re.search(r"(?:법정|법률|법령|기준값|허용|상한|의무)", note):
        return "법정기준"
    return None


def _suggestions_for_sentence(
    sentence: dict[str, Any],
    claims: list[dict[str, Any]],
    published_at: str,
) -> dict[str, Any]:
    text = str(sentence.get("원문 문장") or "")
    indicator_scopes = []
    source_regions = []
    period_contexts = []
    boundaries = []
    for index, claim in enumerate(claims, start=1):
        eligibility = str(claim.get("검증대상 gold") or "")
        indicator = str(claim.get("indicator gold") or "").strip()
        span = _span_suggestion(text, indicator)
        if eligibility == "KOSIS_CANDIDATE" and indicator and indicator != "없음":
            scope_id = f"SCOPE-SUG-{index:02d}"
            indicator_scopes.append({
                "scope_id": scope_id,
                "indicator_text_suggestion": indicator,
                "source_span_suggestion": span,
                "attribution_suggestion": (
                    "이 문장에서 도입" if span else "앞에서 상속"
                ),
                "suggestion_only": True,
            })
            target = str(claim.get("target value") or "")
            target_start = text.find(target) if target else -1
            boundaries.append({
                "scope_id": scope_id,
                "target_value": target,
                "candidate_span_id": claim.get("candidate span ID"),
                "boundary_start_suggestion": (
                    target_start if target_start >= 0 else None
                ),
                "boundary_end_suggestion": (
                    target_start + len(target) if target_start >= 0 else None
                ),
                "suggestion_only": True,
            })
        subtype = _source_subtype_suggestion(claim)
        if subtype:
            source_regions.append({
                "region_id": f"REGION-SUG-{index:02d}",
                "source_subtype_suggestion": subtype,
                "source_span_suggestion": None,
                "suggestion_only": True,
            })
        period = str(claim.get("period gold") or "").strip()
        if eligibility == "KOSIS_CANDIDATE" and period and period != "없음":
            period_contexts.append({
                "period_raw_suggestion": period,
                "period_absolute_suggestion": normalize_time_ref(
                    period, published_at
                ),
                "published_at": published_at,
                "suggestion_only": True,
            })
    return {
        "sentence_review_id": sentence.get("문장검토ID"),
        "article_idx": str(sentence.get("article_idx") or ""),
        "sentence_id": sentence.get("sentence_id"),
        "label_provenance": "AUTO_DERIVED",
        "indicator_scopes_suggestion": indicator_scopes,
        "source_regions_suggestion": source_regions,
        "period_contexts_suggestion": period_contexts,
        "clause_value_boundaries_suggestion": boundaries,
        "dominant_region_suggestion": None,
        "warning": "suggestion only; do not use as gold",
    }


def build_l2_review_artifact(
    snapshot_path: Path,
    scaffold_path: Path,
    article_input_path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    sentence_rows = _matrix_records(snapshot, "sentence_review")
    claim_rows = _matrix_records(snapshot, "claim_gold")
    scaffold = _read_jsonl(scaffold_path)
    articles = {
        str(row.get("article_idx") or ""): row
        for row in _read_jsonl(article_input_path)
    }
    sentence_by_id = {
        str(row.get("문장검토ID") or ""): row
        for row in sentence_rows
    }
    claims_by_sentence: dict[tuple[str, int], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for claim in claim_rows:
        article_idx = str(claim.get("article_idx") or "")
        sentence_id = claim.get("sentence_id")
        if article_idx and isinstance(sentence_id, int):
            claims_by_sentence[(article_idx, sentence_id)].append(claim)

    selected = [
        row for row in scaffold
        if (
            row.get("requires_human_region_review") is True
            or row.get("source_region_gold") in OUT_OF_SCOPE_REVIEW_REGIONS
        )
    ]
    human_rows = []
    suggestion_rows = []
    reason_counts: Counter[str] = Counter()
    for scaffold_row in selected:
        review_id = str(scaffold_row.get("sentence_review_id") or "")
        sentence = sentence_by_id[review_id]
        article_idx = str(scaffold_row.get("article_idx") or "")
        sentence_id = scaffold_row.get("sentence_id")
        source_region_gold = scaffold_row.get("source_region_gold")
        if source_region_gold == "OUT_OF_SCOPE_UNSPECIFIED":
            reason = "OUT_OF_SCOPE_SUBTYPE"
        elif source_region_gold in {
            "MIXED",
            "NO_VERIFIABLE_NUMERIC_CLAIM",
        }:
            reason = "REGION_CONFLICT_RESOLUTION"
        elif (
            scaffold_row.get("derivation_status")
            == "MULTI_INDICATOR_EXPLICIT"
        ):
            reason = "MULTI_INDICATOR_BOUNDARY"
        else:
            reason = "CONTEXT_REGION_INHERITANCE"
        reason_counts[reason] += 1
        article = articles.get(article_idx, {})
        published_at = str(article.get("date") or "")
        claims = claims_by_sentence.get((article_idx, sentence_id), [])
        human_rows.append({
            "sentence_review_id": review_id,
            "article_idx": article_idx,
            "sentence_id": sentence_id,
            "title": sentence.get("기사제목"),
            "published_at": published_at,
            "text": sentence.get("원문 문장"),
            "value_candidates_text": sentence.get("자동 value candidates"),
            "value_candidate_span_ids": _value_candidate_span_ids(
                sentence,
                claims,
            ),
            "review_reason": reason,
            "indicator_scopes_json": "",
            "source_regions_json": "",
            "period_contexts_json": "",
            "clause_value_boundaries_json": "",
            "dominant_region_decision": "",
            "label_provenance": "UNREVIEWED",
            "review_status": "미검토",
            "reviewer_note": "",
        })
        suggestion_rows.append(
            _suggestions_for_sentence(
                sentence,
                claims,
                published_at,
            )
        )

    selected_ids = {
        str(row.get("sentence_review_id") or "") for row in selected
    }
    context_rows = []
    for scaffold_row in sorted(
        scaffold,
        key=lambda row: (
            int(str(row.get("article_idx") or "0")),
            int(row.get("sentence_id") or 0),
        ),
    ):
        review_id = str(scaffold_row.get("sentence_review_id") or "")
        sentence = sentence_by_id[review_id]
        article_idx = str(scaffold_row.get("article_idx") or "")
        sentence_id = int(scaffold_row.get("sentence_id") or 0)
        is_review_target = review_id in selected_ids
        scope_id = ""
        region_id = ""
        source_subtype = ""
        indicator_label = ""
        if not is_review_target:
            scope_id, region_id = _automatic_context_ids(
                article_idx,
                sentence_id,
            )
            source_subtype = "공식집계"
            scopes = scaffold_row.get("indicator_scope_gold") or []
            indicator_label = " | ".join(str(value) for value in scopes)
        context_rows.append({
            "sentence_review_id": review_id,
            "article_idx": article_idx,
            "sentence_id": sentence_id,
            "text": sentence.get("원문 문장"),
            "row_kind": "검토대상" if is_review_target else "자동확정",
            "region_id": region_id,
            "scope_id": scope_id,
            "indicator_label": indicator_label,
            "source_subtype": source_subtype,
            "derivation_status": scaffold_row.get("derivation_status"),
            "disagree_flag": "",
            "reviewer_note": "",
        })

    contract = {
        "contract_version": "l2_sentence_regions_v3",
        "artifact_status": "DRAFT_NEEDS_HUMAN_ADJUDICATION",
        "review_rows": len(human_rows),
        "review_reason_counts": dict(reason_counts),
        "human_file_prefills_suggestions": False,
        "suggestions_are_gold": False,
        "id_uniqueness_scope": ID_UNIQUENESS_SCOPE,
        "scope_id_format": SCOPE_ID_FORMAT,
        "region_id_format": REGION_ID_FORMAT,
        "context_rows": len(context_rows),
        "context_row_kind_counts": dict(
            Counter(row["row_kind"] for row in context_rows)
        ),
        "source_region_subtypes": list(SOURCE_REGION_SUBTYPES),
        "scope_attribution_types": list(SCOPE_ATTRIBUTION_TYPES),
        "dominance_values": list(DOMINANCE_VALUES),
        "label_provenance_values": list(LABEL_PROVENANCE_VALUES),
        "indicator_scope_schema": {
            "required": [
                "scope_id",
                "indicator_label",
                "source_span_text",
                "attribution_type",
            ],
            "optional": ["occurrence_index"],
            "derived": ["source_char_start", "source_char_end"],
            "attribution_type_enum": list(SCOPE_ATTRIBUTION_TYPES),
        },
        "source_region_schema": {
            "required": [
                "region_id",
                "source_subtype",
                "source_span_text",
            ],
            "optional": ["occurrence_index"],
            "derived": ["source_char_start", "source_char_end"],
            "source_subtype_enum": list(SOURCE_REGION_SUBTYPES),
        },
        "period_context_schema": {
            "required": [
                "period_raw",
                "period_absolute",
                "published_at",
                "source_span_text",
            ],
            "optional": ["occurrence_index"],
            "derived": ["source_char_start", "source_char_end"],
            "absolute_reference": "published_at",
        },
        "boundary_schema": {
            "required": [
                "scope_id",
                "boundary_type",
                "target_value_span_ids",
            ],
            "optional": ["clause_text", "occurrence_index"],
            "derived": ["char_start", "char_end"],
            "boundary_type_enum": ["절", "값"],
        },
        "span_resolution_rule": (
            "Reviewers enter source_span_text only; char offsets are derived "
            "deterministically by locating that text in the sentence. A span "
            "that is absent from the sentence is an error, and a span that "
            "occurs more than once requires occurrence_index or a longer "
            "span. Offsets are never accepted as human input."
        ),
        "boundary_reference_rule": (
            "target_value_span_ids must be listed in the row's "
            "value_candidate_span_ids; clause char offsets are derived from "
            "clause_text when supplied"
        ),
        "human_confirmation_rule": (
            "label_provenance must be HUMAN_CONFIRMED only after all required "
            "JSON fields and dominance decision are reviewed"
        ),
        "reference_integrity_rule": (
            "dominant_region_decision must be '지배 없음' or a region_id "
            "defined in the same article"
        ),
        "disagreement_rule": (
            "disagree_flag on an AUTO_DERIVED context row is reported "
            "separately at Gate L2 and requires reviewer_note"
        ),
    }
    if len(human_rows) != 98:
        raise ValueError(f"expected 98 L2 review rows, got {len(human_rows)}")
    if len(context_rows) != 117:
        raise ValueError(
            f"expected 117 L2 context rows, got {len(context_rows)}"
        )
    if Counter(row["row_kind"] for row in context_rows) != {
        "검토대상": 98,
        "자동확정": 19,
    }:
        raise ValueError("unexpected L2 context row-kind distribution")
    return human_rows, suggestion_rows, context_rows, contract


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--scaffold", type=Path, required=True)
    parser.add_argument("--article-input", type=Path, required=True)
    parser.add_argument("--human-jsonl", type=Path, required=True)
    parser.add_argument("--suggestions-jsonl", type=Path, required=True)
    parser.add_argument("--context-jsonl", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    human, suggestions, context, contract = build_l2_review_artifact(
        args.snapshot,
        args.scaffold,
        args.article_input,
    )
    _write_jsonl(args.human_jsonl, human)
    _write_jsonl(args.suggestions_jsonl, suggestions)
    _write_jsonl(args.context_jsonl, context)
    args.contract.parent.mkdir(parents=True, exist_ok=True)
    args.contract.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(contract, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
