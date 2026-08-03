"""Evaluate L5 soft-gate routing curves without selecting an operating point."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


ANCHOR_CODES = frozenset({
    "INDICATOR_ANCHOR_NOT_FOUND_IN_VALUE_SENTENCE",
    "VALUE_OUTSIDE_INDICATOR_SCOPE",
})
GROUNDING_CODES = frozenset({
    "INDICATOR_NORM_NOT_GROUNDED_IN_CLAIM_SENTENCES",
})
UMBRELLA_CODES = frozenset({"SEMANTIC_VALIDATION_BLOCKED"})
THRESHOLDS = (1.0, 0.9, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _error_codes(row: dict[str, Any]) -> set[str]:
    automatic = row.get("automatic") or {}
    result = set()
    for field in ("semantic_errors", "binding_errors", "scope_errors"):
        result.update(str(code) for code in automatic.get(field) or [])
    return result


def score_routing_row(
    row: dict[str, Any],
    *,
    soften_anchor: bool,
    soften_grounding: bool,
) -> dict[str, Any]:
    automatic = row.get("automatic") or {}
    prediction = row.get("prediction") or {}
    if not prediction.get("detected") or automatic.get("action") == "MISSED":
        return {"score": 0.0, "hard_blocked": True, "soft_groups": []}
    if automatic.get("action") == "PASS":
        return {"score": 1.0, "hard_blocked": False, "soft_groups": []}

    codes = _error_codes(row)
    soft_groups = []
    remaining = set(codes)
    if soften_anchor and remaining & ANCHOR_CODES:
        remaining -= ANCHOR_CODES
        soft_groups.append("ANCHOR_SCOPE")
    if soften_grounding and remaining & GROUNDING_CODES:
        remaining -= GROUNDING_CODES
        soft_groups.append("INDICATOR_GROUNDING")
    if soft_groups:
        remaining -= UMBRELLA_CODES
    if remaining or not soft_groups:
        return {
            "score": 0.0,
            "hard_blocked": True,
            "soft_groups": soft_groups,
            "remaining_hard_codes": sorted(remaining or codes),
        }

    score = 1.0
    if "ANCHOR_SCOPE" in soft_groups:
        score -= 0.25
    if "INDICATOR_GROUNDING" in soft_groups:
        score -= 0.35
    return {
        "score": score,
        "hard_blocked": False,
        "soft_groups": soft_groups,
        "remaining_hard_codes": [],
    }


def _metrics(
    rows: list[dict[str, Any]],
    scored: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    routed_review_ids = []
    for row, score in zip(rows, scored):
        gold_positive = (
            str((row.get("gold") or {}).get("eligibility") or "")
            == "KOSIS_CANDIDATE"
        )
        predicted = (
            not score["hard_blocked"] and score["score"] >= threshold
        )
        if predicted:
            routed_review_ids.append(row.get("review_id"))
        if predicted and gold_positive:
            tp += 1
        elif predicted:
            fp += 1
        elif gold_positive:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall else 0.0
    )
    return {
        "threshold": threshold,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "routed_review_ids": routed_review_ids,
    }


def sweep_condition(
    rows: list[dict[str, Any]],
    *,
    soften_anchor: bool,
    soften_grounding: bool,
) -> list[dict[str, Any]]:
    scored = [
        score_routing_row(
            row,
            soften_anchor=soften_anchor,
            soften_grounding=soften_grounding,
        )
        for row in rows
    ]
    return [
        _metrics(rows, scored, threshold) for threshold in THRESHOLDS
    ]


def _verify_ambiguous_retry(path: Path) -> dict[str, Any]:
    audit = json.loads(path.read_text(encoding="utf-8"))
    retry = audit.get("ambiguous_retry_audit") or {}
    required = {
        "targets": 8,
        "resolved_non_ambiguous": 8,
        "still_ambiguous": 0,
        "missing_from_retry": 0,
        "current_candidate_class_ambiguous": 0,
    }
    actual = {key: retry.get(key) for key in required}
    if actual != required:
        raise ValueError(
            f"AMBIGUOUS retry verification failed: {actual}"
        )
    return {
        **actual,
        "excluded_from_softening": True,
        "decision": (
            "CANDIDATE_CLASS_AMBIGUOUS is not a Phase 1 soft-gate target"
        ),
    }


def build_sweep_report(
    condition_paths: dict[str, Path],
    ambiguous_audit_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ambiguous = _verify_ambiguous_retry(ambiguous_audit_path)
    curve_rows = []
    condition_reports = {}
    for condition, path in condition_paths.items():
        rows = _read_jsonl(path)
        rounds = {
            "round1_anchor_scope": {
                "soften_anchor": True,
                "soften_grounding": False,
            },
            "round2_anchor_plus_grounding": {
                "soften_anchor": True,
                "soften_grounding": True,
            },
        }
        condition_reports[condition] = {}
        for round_name, options in rounds.items():
            curve = sweep_condition(rows, **options)
            compact = [
                {key: value for key, value in point.items()
                 if key != "routed_review_ids"}
                for point in curve
            ]
            condition_reports[condition][round_name] = compact
            for point in compact:
                curve_rows.append({
                    "lexical_condition": condition,
                    "round": round_name,
                    **point,
                })
    report = {
        "contract_version": "r17_l5_soft_gate_sweep_v1",
        "operating_threshold_selected": False,
        "selection_warning": (
            "Development-gold curves are descriptive; select no production "
            "threshold before the new stratum holdout is frozen."
        ),
        "score_policy": {
            "existing_pass": 1.0,
            "anchor_scope_penalty_once": 0.25,
            "indicator_grounding_penalty": 0.35,
            "anchor_claim_and_observation_codes_count_as_one_group": True,
        },
        "ambiguous_retry_verification": ambiguous,
        "conditions": condition_reports,
    }
    return report, curve_rows


def _write_svg(path: Path, rows: list[dict[str, Any]]) -> None:
    width, height = 900, 560
    left, top, plot_w, plot_h = 90, 60, 720, 410
    colors = {
        ("LEXICAL_ON", "round1_anchor_scope"): "#1F4E78",
        ("LEXICAL_ON", "round2_anchor_plus_grounding"): "#5B9BD5",
        ("LEXICAL_OFF", "round1_anchor_scope"): "#A64D79",
        ("LEXICAL_OFF", "round2_anchor_plus_grounding"): "#E06666",
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(
            (row["lexical_condition"], row["round"]), []
        ).append(row)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="90" y="30" font-size="20" font-family="sans-serif" '
        'font-weight="bold">r17 Phase 1 routing Precision–Recall curve</text>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" '
        'y2="{top + plot_h}" stroke="#333"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" '
        f'y2="{top + plot_h}" stroke="#333"/>',
    ]
    for tick in range(0, 11):
        value = tick / 10
        x = left + value * plot_w
        y = top + (1 - value) * plot_h
        lines.extend([
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" '
            f'y2="{top + plot_h}" stroke="#E6E6E6"/>',
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" '
            f'y2="{y:.1f}" stroke="#E6E6E6"/>',
            f'<text x="{x:.1f}" y="{top + plot_h + 24}" '
            f'text-anchor="middle" font-size="11">{value:.1f}</text>',
            f'<text x="{left - 12}" y="{y + 4:.1f}" '
            f'text-anchor="end" font-size="11">{value:.1f}</text>',
        ])
    lines.extend([
        f'<text x="{left + plot_w / 2}" y="{height - 45}" '
        'text-anchor="middle" font-size="14">Recall</text>',
        f'<text x="24" y="{top + plot_h / 2}" text-anchor="middle" '
        'font-size="14" transform="rotate(-90 24 '
        f'{top + plot_h / 2})">Precision</text>',
    ])
    legend_y = 80
    for key, points in grouped.items():
        unique = []
        for point in points:
            coordinate = (point["recall"], point["precision"])
            if coordinate not in unique:
                unique.append(coordinate)
        coords = " ".join(
            f"{left + recall * plot_w:.1f},{top + (1 - precision) * plot_h:.1f}"
            for recall, precision in unique
        )
        color = colors[key]
        lines.append(
            f'<polyline points="{coords}" fill="none" stroke="{color}" '
            'stroke-width="3"/>'
        )
        for recall, precision in unique:
            lines.append(
                f'<circle cx="{left + recall * plot_w:.1f}" '
                f'cy="{top + (1 - precision) * plot_h:.1f}" r="5" '
                f'fill="{color}"/>'
            )
        label = f"{key[0]} · {key[1]}"
        lines.extend([
            f'<line x1="620" y1="{legend_y}" x2="650" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="3"/>',
            f'<text x="660" y="{legend_y + 4}" font-size="11">{label}</text>',
        ])
        legend_y += 22
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lexical-on", type=Path, required=True)
    parser.add_argument("--lexical-off", type=Path, required=True)
    parser.add_argument("--ambiguous-audit", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report, curve_rows = build_sweep_report(
        {
            "LEXICAL_ON": args.lexical_on,
            "LEXICAL_OFF": args.lexical_off,
        },
        args.ambiguous_audit,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "routing_pr_sweep.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (args.output_dir / "routing_pr_curve.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(curve_rows[0]))
        writer.writeheader()
        writer.writerows(curve_rows)
    score_audit_rows = []
    round_options = {
        "round1_anchor_scope": {
            "soften_anchor": True,
            "soften_grounding": False,
        },
        "round2_anchor_plus_grounding": {
            "soften_anchor": True,
            "soften_grounding": True,
        },
    }
    for condition, prediction_path in {
        "LEXICAL_ON": args.lexical_on,
        "LEXICAL_OFF": args.lexical_off,
    }.items():
        prediction_rows = _read_jsonl(prediction_path)
        for round_name, options in round_options.items():
            for row in prediction_rows:
                score = score_routing_row(row, **options)
                score_audit_rows.append({
                    "lexical_condition": condition,
                    "round": round_name,
                    "review_id": row.get("review_id"),
                    "article_idx": row.get("article_idx"),
                    "gold_eligibility": (
                        row.get("gold") or {}
                    ).get("eligibility"),
                    "automatic_action": (
                        row.get("automatic") or {}
                    ).get("action"),
                    "error_codes": sorted(_error_codes(row)),
                    **score,
                })
    (args.output_dir / "routing_score_audit.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in score_audit_rows
        ),
        encoding="utf-8",
    )
    _write_svg(args.output_dir / "routing_pr_curve.svg", curve_rows)
    print(json.dumps({
        "curve_rows": len(curve_rows),
        "ambiguous": report["ambiguous_retry_verification"],
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
