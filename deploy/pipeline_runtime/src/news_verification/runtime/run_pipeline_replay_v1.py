"""Replay the real-data R4-C1 vertical slice without external calls.

The runner consumes immutable article/L2-derived routing, official-search,
profile-cache and cached KOSIS query-response artefacts.  Cached cells are
joined only by the complete Param API query; target IDs and frozen expected
labels are never selection authority.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.news_verification.runtime.deterministic_comparator import compare_values, parse_korean_number
from src.news_verification.runtime.r4c1_article_context import with_article_date_context
from src.news_verification.runtime.r4c1_claim_core_v2 import build_claim_core_v2
from src.news_verification.runtime.r4c1_profile_cache import PersistentProfileCache
from src.news_verification.runtime.r4c1_projection_v2 import (
    CandidateAssignment,
    CandidateProjection,
    project_candidate_v2,
    validate_target_v2,
)
from src.news_verification.runtime.run_layer_stack import run_stack


CONTRACT_VERSION = "kosis-real-data-replay-pipeline-v1"
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "configs/pipeline_replay_v1.json"


class ReplayContractError(ValueError):
    """Raised when a pinned input or replay invariant is violated."""


def _compose_target_id(split: str, article_idx: Any, value_span_id: Any) -> str:
    return f"{split}:{article_idx}:{value_span_id}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(dict(row)) + b"\n" for row in rows)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _unique(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        identity = str(row.get(key) or "")
        if not identity or identity in result:
            raise ReplayContractError(f"missing or duplicate {key}: {identity!r}")
        result[identity] = row
    return result


def _routed_index(rows: Iterable[Mapping[str, Any]], split: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        target_id = _compose_target_id(split, row.get("article_idx"), row.get("value_span_id"))
        if target_id in indexed:
            raise ReplayContractError(f"duplicate routed target: {target_id}")
        row["target_id"] = target_id
        indexed[target_id] = row
    return indexed


def _load_config(
    config_path: Path, asset_root: str | Path | None = None
) -> tuple[dict[str, Any], dict[str, Path]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("contract_version") != CONTRACT_VERSION:
        raise ReplayContractError("config contract version mismatch")
    if config.get("mode") != "replay":
        raise ReplayContractError("release config must default to replay mode")
    base = Path(asset_root).resolve() if asset_root is not None else ROOT
    paths: dict[str, Path] = {}
    for name, spec in config.get("inputs", {}).items():
        path = Path(str(spec.get("path") or ""))
        path = path if path.is_absolute() else base / path
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = _sha256_file(path)
        if actual != str(spec.get("sha256") or ""):
            raise ReplayContractError(f"input SHA mismatch: {name}")
        paths[name] = path
    required = {
        "article_snapshot", "article_source", "l2_snapshot", "routed_snapshot", "search_snapshot",
        "profile_cache", "cell_cache",
    }
    if set(paths) != required:
        raise ReplayContractError(f"input set mismatch: {sorted(set(paths) ^ required)}")
    return config, paths


def _query_key(query: Mapping[str, Any]) -> bytes:
    obj_levels = query.get("obj_levels")
    if not isinstance(obj_levels, Mapping):
        raise ReplayContractError("query obj_levels must be an object")
    normalized = {
        "org_id": str(query.get("org_id") or ""),
        "tbl_id": str(query.get("tbl_id") or ""),
        "itm_id": str(query.get("itm_id") or ""),
        "prd_se": str(query.get("prd_se") or ""),
        "start_prd_de": str(query.get("start_prd_de") or ""),
        "end_prd_de": str(query.get("end_prd_de") or ""),
        "obj_levels": {str(k): str(v) for k, v in sorted(obj_levels.items())},
    }
    if any(not normalized[key] for key in (
        "org_id", "tbl_id", "itm_id", "prd_se", "start_prd_de", "end_prd_de"
    )) or not normalized["obj_levels"]:
        raise ReplayContractError("query plan is incomplete")
    return _canonical_bytes(normalized)


def build_cell_cache_index(rows: Iterable[Mapping[str, Any]]) -> dict[bytes, list[dict[str, Any]]]:
    """Index only query/response evidence; ignore expected labels and target IDs."""
    index: dict[bytes, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for operand in row.get("operands") or []:
            if not isinstance(operand, Mapping) or not isinstance(operand.get("query"), Mapping):
                continue
            evidence = {
                key: operand.get(key)
                for key in (
                    "query", "cell_status", "response_row_count", "observed",
                    "period_encoding_evidence", "error_type", "kosis_err_code",
                    "cell_available_period_range",
                )
                if key in operand
            }
            evidence["evidence_sha256"] = hashlib.sha256(_canonical_bytes(evidence)).hexdigest()
            bucket = index[_query_key(operand["query"])]
            # The frozen verification ledger can repeat one official response
            # for separate claims that produce the exact same Param query.
            # Collapse only byte-equivalent evidence; conflicting receipts
            # remain multiple and fail closed in ``match_cached_cell``.
            if not any(existing["evidence_sha256"] == evidence["evidence_sha256"] for existing in bucket):
                bucket.append(evidence)
    return dict(index)


def match_cached_cell(
    query_plan: Mapping[str, Any], cache_index: Mapping[bytes, list[dict[str, Any]]]
) -> dict[str, Any]:
    matches = list(cache_index.get(_query_key(query_plan), ()))
    if not matches:
        return {"status": "CACHE_QUERY_MISMATCH", "matched_entries": 0}
    if len(matches) != 1:
        return {"status": "CACHE_QUERY_AMBIGUOUS", "matched_entries": len(matches)}
    evidence = matches[0]
    status = str(evidence.get("cell_status") or "")
    if status == "NO_CELL":
        return {"status": "NO_CELL", "matched_entries": 1, "evidence": evidence}
    if status != "CELL_RESOLVED":
        return {"status": "CACHE_CELL_INVALID", "matched_entries": 1, "evidence": evidence}
    count = evidence.get("response_row_count")
    if count != 1:
        return {
            "status": "MULTIPLE_CELLS" if isinstance(count, int) and count > 1 else "NO_CELL",
            "matched_entries": 1,
            "evidence": evidence,
        }
    observed = evidence.get("observed")
    if not isinstance(observed, Mapping) or observed.get("DT") in (None, ""):
        return {"status": "CACHE_CELL_INVALID", "matched_entries": 1, "evidence": evidence}
    return {"status": "CELL_RESOLVED", "matched_entries": 1, "evidence": evidence}


def _assignment_for_resolution(
    projections: Iterable[CandidateProjection], chosen_table_key: str | None
) -> CandidateAssignment | None:
    found = [
        assignment
        for projection in projections
        if projection.table_key == chosen_table_key
        for assignment in projection.assignments
    ]
    return found[0] if len(found) == 1 else None


def _binding_summary(assignment: CandidateAssignment | None) -> dict[str, Any]:
    if assignment is None:
        return {"item": None, "dimensions": [], "period": None, "unit": None}
    result: dict[str, Any] = {"item": None, "dimensions": [], "period": None, "unit": None}
    for binding in assignment.bindings:
        summary = {
            "axis_id": binding.axis_id,
            "value_id": binding.value_id,
            "label": binding.evidence.get("profile_label"),
            "inventory_path": binding.evidence.get("profile_inventory_path"),
            "claim_provenance": binding.evidence.get("claim_provenance"),
            "basis": binding.binding_basis,
        }
        if binding.axis_kind == "ITEM":
            result["item"] = summary
        elif binding.axis_kind == "DIMENSION":
            axis = binding.evidence.get("axis_evidence") or {}
            result["dimensions"].append({**summary, "axis_label": axis.get("profile_label")})
        elif binding.axis_kind == "PERIOD":
            result["period"] = summary
        elif binding.axis_kind == "UNIT":
            result["unit"] = summary
    return result


_REASON_TEXT = {
    "NO_CANDIDATES": "관련 KOSIS 통계표 후보를 찾지 못했습니다.",
    "PROFILE_UNAVAILABLE": "후보 통계표의 공식 메타데이터를 확인할 수 없습니다.",
    "PROFILE_INCOMPLETE": "후보 통계표의 ITEM 또는 DIMENSION 정보가 불완전합니다.",
    "POPULATION_UNBOUND": "기사의 대상을 통계표 분류값에 유일하게 연결하지 못했습니다.",
    "REGION_UNBOUND": "기사의 지역을 통계표 분류값에 연결하지 못했습니다.",
    "PERIOD_INVALID": "기사의 기간을 손실 없이 구조화하지 못했습니다.",
    "PERIOD_UNKNOWN": "기사에서 검증 기간을 확정하지 못했습니다.",
    "PERIOD_FREQUENCY_MISMATCH": "기사와 통계표의 집계 주기가 다릅니다.",
    "PERIOD_OUT_OF_RANGE": "기사 기간이 통계표 수록 범위 밖입니다.",
    "UNIT_MISMATCH": "기사 단위와 선택된 ITEM의 공식 단위가 다릅니다.",
    "MULTIPLE_COMPATIBLE_SERIES": "둘 이상의 통계계열이 가능해 하나를 선택할 수 없습니다.",
    "NO_COMPATIBLE_SERIES": "주장을 검증할 수 있는 완전한 통계계열을 찾지 못했습니다.",
    "NO_CELL": "해당 기간과 항목의 KOSIS 셀이 존재하지 않습니다.",
    "CACHE_QUERY_MISMATCH": "Query Plan과 정확히 일치하는 보관 응답이 없습니다.",
    "CACHE_QUERY_AMBIGUOUS": "동일 Query Plan의 보관 응답이 둘 이상이라 선택하지 않았습니다.",
    "MULTIPLE_CELLS": "Query Plan이 단일 셀을 특정하지 못했습니다.",
    "CACHE_CELL_INVALID": "보관된 KOSIS 셀 응답이 완전하지 않습니다.",
    "CLAIM_VALUE_INVALID": "기사 수치를 결정론적으로 해석하지 못했습니다.",
}


def _claim_view(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row.get("retrieval_fields") or {}
    return {
        "sentence": row.get("sentence_text", ""),
        "value_text": row.get("value_text", ""),
        "unit": row.get("value_unit", ""),
        "indicator": fields.get("indicator") or row.get("indicator_label") or "",
        "period": fields.get("period_raw") or row.get("period_raw") or "",
    }


def _unverifiable(
    target_id: str, routed: Mapping[str, Any], reason: str, *, source: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    detail = _REASON_TEXT.get(reason, "현재 근거만으로는 안전하게 검증할 수 없습니다.")
    return {
        "target_id": target_id,
        "verdict": "UNVERIFIABLE",
        "reason": reason,
        "reason_detail": detail,
        "claim": _claim_view(routed),
        "official": {},
        "comparison": None,
        "source": dict(source or {"org": "KOSIS", "evidence_type": "NONE"}),
        "explanation": detail,
    }


def _answer_from_cell(
    target_id: str,
    routed: Mapping[str, Any],
    resolution: Any,
    assignment: CandidateAssignment,
    profile: Mapping[str, Any],
    cell_match: Mapping[str, Any],
    cell_cache_sha256: str,
) -> dict[str, Any]:
    if cell_match["status"] != "CELL_RESOLVED":
        return _unverifiable(
            target_id,
            routed,
            str(cell_match["status"]),
            source={
                "org": "KOSIS",
                "evidence_type": "CACHED_OFFICIAL_QUERY_RESPONSE",
                "query": resolution.query_plan,
                "cache_sha256": cell_cache_sha256,
            },
        )
    parsed = parse_korean_number(str(routed.get("value_text") or ""))
    if parsed is None:
        return _unverifiable(target_id, routed, "CLAIM_VALUE_INVALID")
    observed = cell_match["evidence"]["observed"]
    try:
        official_value = float(observed["DT"])
    except (TypeError, ValueError):
        return _unverifiable(target_id, routed, "CACHE_CELL_INVALID")
    binding = _binding_summary(assignment)
    official_unit = str((binding.get("unit") or {}).get("label") or "")
    comparison = compare_values(
        parsed[0],
        official_value,
        article_unit=parsed[1],
        official_unit=official_unit,
        article_value_text=str(routed.get("value_text") or ""),
        use_unit_conversion=True,
        use_precision_tolerance=True,
    )
    verdict = "VERIFIED" if comparison["match"] else "REFUTED"
    article_display = str(routed.get("value_text") or "")
    official_display = f"{observed['DT']}{official_unit}"
    explanation = (
        f"기사 수치 {article_display}는 KOSIS '{profile.get('tbl_name', '')}' "
        f"({observed.get('PRD_DE', '')}) 공식 수치 {official_display}과 일치합니다."
        if verdict == "VERIFIED"
        else f"기사 수치 {article_display}는 KOSIS 공식 수치 {official_display}과 다릅니다."
    )
    return {
        "target_id": target_id,
        "verdict": verdict,
        "reason": None,
        "reason_detail": "",
        "claim": _claim_view(routed),
        "official": {
            "value": official_value,
            "value_text": str(observed["DT"]),
            "unit": official_unit,
            "period": observed.get("PRD_DE", ""),
            "table_key": resolution.chosen_table_key,
            "table": profile.get("tbl_name", ""),
            "item": binding.get("item"),
            "dimensions": binding.get("dimensions"),
        },
        "comparison": comparison,
        "source": {
            "org": "KOSIS",
            "evidence_type": "CACHED_OFFICIAL_QUERY_RESPONSE",
            "query": resolution.query_plan,
            "cache_sha256": cell_cache_sha256,
            "evidence_sha256": cell_match["evidence"]["evidence_sha256"],
            "external_call": False,
        },
        "explanation": explanation,
    }


def _article_snapshot(article_source: Path, article_ids: set[str]) -> list[dict[str, Any]]:
    with article_source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result = []
    for article_id in sorted(article_ids, key=int):
        row = rows[int(article_id)]
        text = str(row.get("기사 본문 전체") or "")
        result.append(
            {
                "article_idx": article_id,
                "date": str(row.get("작성일") or ""),
                "article_text": text,
                "article_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    return result


def run_replay(
    config_path: str | Path = DEFAULT_CONFIG, *, asset_root: str | Path | None = None
) -> dict[str, Any]:
    config_path = Path(config_path)
    config, paths = _load_config(config_path, asset_root)
    split = str(config.get("split") or "dev")
    search = _unique(_read_jsonl(paths["search_snapshot"]), "target_id")
    article_anchor = _read_jsonl(paths["article_snapshot"])
    l2_rows = _read_jsonl(paths["l2_snapshot"])
    l2_article_ids = {str(row.get("article_idx") or "") for row in l2_rows}
    if {str(row.get("article_idx") or "") for row in article_anchor} != l2_article_ids:
        raise ReplayContractError("article/L2 population mismatch")
    articles = article_anchor
    if any(
        hashlib.sha256(str(row.get("article_text") or "").encode("utf-8")).hexdigest()
        != str(row.get("article_sha256") or "")
        for row in articles
    ):
        raise ReplayContractError("article snapshot payload SHA mismatch")
    generated_rows = with_article_date_context(
        run_stack(articles, l2_rows), paths["article_source"]
    )
    generated = _routed_index(generated_rows, split)
    frozen_routed = _routed_index(_read_jsonl(paths["routed_snapshot"]), split)
    if not set(search) <= set(generated) or not set(search) <= set(frozen_routed):
        raise ReplayContractError("search target missing from generated/frozen routed handoff")
    consumed_fields = (
        "sentence_text", "value_text", "value_unit", "value_span_id",
        "article_idx", "article_sentence_id", "retrieval_fields",
    )
    drifted = [
        target_id
        for target_id in search
        if any(
            generated[target_id].get(field) != frozen_routed[target_id].get(field)
            for field in consumed_fields
        )
    ]
    if drifted:
        raise ReplayContractError(f"front handoff drift for search targets: {drifted[:5]}")
    routed = {target_id: generated[target_id] for target_id in search}
    routed_rows = list(routed.values())
    target_article_ids = {str(routed[target]["article_idx"]) for target in search}
    if not target_article_ids <= l2_article_ids:
        raise ReplayContractError("target article missing from L2 snapshot")

    cache = PersistentProfileCache(paths["profile_cache"])
    table_keys = sorted({
        str(candidate.get("table_key") or "")
        for row in search.values()
        for candidate in (row.get("retrieval", {}).get("candidates") or [])
        if candidate.get("table_key")
    })
    profiles: dict[str, dict[str, Any]] = {}
    profile_shas: dict[str, str] = {}
    cache_status: Counter[str] = Counter()
    for table_key in table_keys:
        lookup = cache.lookup(
            table_key, max_age_seconds=float(config["profile_max_age_seconds"])
        )
        cache_status[lookup.status] += 1
        if lookup.profile is not None:
            profiles[table_key] = lookup.profile
            profile_shas[table_key] = str(lookup.profile_sha256 or "")

    cell_rows = json.loads(paths["cell_cache"].read_text(encoding="utf-8"))
    if not isinstance(cell_rows, list):
        raise ReplayContractError("cell cache must be a JSON array")
    cell_index = build_cell_cache_index(cell_rows)
    cell_cache_sha = _sha256_file(paths["cell_cache"])
    stage_rows: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    outcome_counts: Counter[str] = Counter()
    cell_counts: Counter[str] = Counter()
    verdict_counts: Counter[str] = Counter()

    for target_id, search_row in sorted(search.items()):
        routed_row = routed[target_id]
        core = build_claim_core_v2(routed_row)
        candidates = list(search_row.get("retrieval", {}).get("candidates") or [])
        projections = [
            project_candidate_v2(core, profiles.get(str(candidate.get("table_key") or "")))
            for candidate in candidates
        ]
        resolution = validate_target_v2(projections) if projections else None
        if resolution is None:
            outcome, reason = "HOLD", "NO_CANDIDATES"
            query_plan = chosen_table = assignment = None
        else:
            outcome, reason = resolution.outcome, resolution.hold_reason
            query_plan, chosen_table = resolution.query_plan, resolution.chosen_table_key
            assignment = _assignment_for_resolution(projections, chosen_table)
        outcome_counts[outcome if outcome == "QUERY_READY" else str(reason)] += 1
        cell_match: dict[str, Any] | None = None
        if outcome == "QUERY_READY":
            if assignment is None or chosen_table not in profiles:
                raise ReplayContractError(f"ready target lacks one assignment: {target_id}")
            cell_match = match_cached_cell(query_plan, cell_index)
            cell_counts[cell_match["status"]] += 1
            answer = _answer_from_cell(
                target_id, routed_row, resolution, assignment, profiles[chosen_table],
                cell_match, cell_cache_sha,
            )
        else:
            answer = _unverifiable(target_id, routed_row, str(reason))
        verdict_counts[answer["verdict"]] += 1
        answers.append(answer)
        stage_rows.append(
            {
                "target_id": target_id,
                "front": {
                    "mode": "FROZEN_ARTICLE_L2_DETERMINISTIC_L3_L5_REPLAY",
                    "article_idx": str(routed_row.get("article_idx") or ""),
                    "article_date_provenance": routed_row.get("article_date_provenance"),
                    "l2_snapshot_sha256": config["inputs"]["l2_snapshot"]["sha256"],
                    "routed_snapshot_sha256": config["inputs"]["routed_snapshot"]["sha256"],
                },
                "claim_core": asdict(core),
                "retrieval": {
                    "source": "KOSIS_INTEGRATED_SEARCH_API_SNAPSHOT",
                    "candidate_membership": [str(c.get("table_key") or "") for c in candidates],
                    "candidate_count": len(candidates),
                },
                "profiles": [
                    {
                        "table_key": str(c.get("table_key") or ""),
                        "profile_sha256": profile_shas.get(str(c.get("table_key") or "")),
                        "available": str(c.get("table_key") or "") in profiles,
                    }
                    for c in candidates
                ],
                "projections": [asdict(projection) for projection in projections],
                "resolution": asdict(resolution) if resolution is not None else {
                    "outcome": "HOLD", "hold_reason": "NO_CANDIDATES", "query_plan": None
                },
                "cell_cache_match": cell_match,
                "final_answer": answer,
            }
        )

    report = {
        "contract_version": CONTRACT_VERSION,
        "release_id": config["release_id"],
        "pipeline_mode": "replay",
        "front_contract": {
            "articles": len(articles),
            "l2_rows": len(l2_rows),
            "generated_routed_rows": len(generated_rows),
            "frozen_routed_rows": len(frozen_routed),
            "target_rows": len(search),
            "target_handoff_drift": len(drifted),
            "execution": "frozen articles + frozen L2 -> deterministic L3-L5; no model call",
        },
        "resolution_distribution": dict(sorted(outcome_counts.items())),
        "cell_distribution": dict(sorted(cell_counts.items())),
        "verdict_distribution": dict(sorted(verdict_counts.items())),
        "query_ready": outcome_counts["QUERY_READY"],
        "external_model_calls": 0,
        "metadata_api_calls": 0,
        "cell_api_calls": 0,
        "profile_cache_status": dict(sorted(cache_status.items())),
        "input_sha256": {
            name: spec["sha256"] for name, spec in config["inputs"].items()
        },
        "gold_or_scoring_inputs_accessed": [],
        "selection_authority": "claim provenance + profile inventory only",
        "cell_join_authority": "complete Param API query only",
    }
    expected = config["expected"]
    observed = {
        "articles": len(articles),
        "targets": len(search),
        "query_ready": outcome_counts["QUERY_READY"],
        "single_cell_resolved": cell_counts["CELL_RESOLVED"],
        "no_cell": cell_counts["NO_CELL"],
        "external_model_calls": 0,
        "metadata_api_calls": 0,
        "cell_api_calls": 0,
    }
    if observed != expected:
        raise ReplayContractError(f"release expectation mismatch: {observed} != {expected}")
    report["release_expectation"] = {"expected": expected, "observed": observed, "passed": True}
    return {"report": report, "stage_rows": stage_rows, "answers": answers, "articles": articles}


def _answers_markdown(answers: Iterable[Mapping[str, Any]]) -> str:
    lines = ["# KOSIS 실제 데이터 재생 검증 결과", ""]
    for answer in answers:
        claim = answer.get("claim") or {}
        lines.extend(
            [
                f"## {answer.get('target_id')}",
                "",
                f"- 판정: `{answer.get('verdict')}`",
                f"- 주장: {claim.get('sentence', '')}",
                f"- 결과: {answer.get('explanation', '')}",
                "",
            ]
        )
    return "\n".join(lines)


def write_replay(
    config_path: str | Path,
    output_root: str | Path,
    *,
    asset_root: str | Path | None = None,
) -> dict[str, Any]:
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite replay output: {output_root}")
    result = run_replay(config_path, asset_root=asset_root)
    payloads = {
        "articles_snapshot.jsonl": _jsonl_bytes(result["articles"]),
        "stage_ledger.jsonl": _jsonl_bytes(result["stage_rows"]),
        "final_answers.jsonl": _jsonl_bytes(result["answers"]),
        "final_answers.md": _answers_markdown(result["answers"]).encode("utf-8"),
    }
    report = dict(result["report"])
    report["output_sha256"] = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
    payloads["run_report.json"] = json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "release_id": report["release_id"],
        "config_sha256": _sha256_file(Path(config_path)),
        "inputs": report["input_sha256"],
        "outputs": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
        "external_calls": {"model": 0, "metadata_api": 0, "cell_api": 0},
    }
    payloads["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    temporary = output_root.with_name(output_root.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary output already exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        for name, data in payloads.items():
            (temporary / name).write_bytes(data)
        os.replace(temporary, output_root)
    except Exception:
        # Preserve partial evidence for diagnosis; a later run must choose a new path.
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--mode", choices=("replay", "live", "oracle-demo"), default="replay")
    args = parser.parse_args(argv)
    if args.mode == "live":
        raise ReplayContractError("live mode requires a separate approved adapter and explicit release")
    if args.mode == "oracle-demo":
        raise ReplayContractError("oracle-demo is isolated in run_pipeline_e2e and is not product replay")
    report = write_replay(args.config, args.output, asset_root=args.asset_root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ReplayContractError",
    "build_cell_cache_index",
    "match_cached_cell",
    "run_replay",
    "write_replay",
]



