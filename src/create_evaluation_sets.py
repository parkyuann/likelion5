"""Create the three evaluation sets used after claim labeling.

Outputs are claim-row CSVs with article-level disjointness:
  1) pilot120: HCX execution/error and output-format checks
  2) validation300: status/class/source-scope stratified comparison set
  3) final500: human gold-label set with at least 30 rows per claim class
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "retrieval_eval_claims_v0_codex.csv"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "evaluation"
SEED = 20260716

HCX_COLUMNS = {
    "hcx_run_status": "pending",
    "hcx_error_type": "",
    "hcx_output_valid": "",
    "hcx_raw_output": "",
    "hcx_latency_ms": "",
    "hcx_notes": "",
}

REVIEW_COLUMNS = {
    "selected_version": "",
    "selection_notes": "",
    "human_review_status": "pending",
    "human_reviewer": "",
}


def choose_one_per_article(frame: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    if n <= 0 or frame.empty:
        return frame.iloc[0:0].copy()
    shuffled = frame.sample(frac=1, random_state=seed)
    return shuffled.drop_duplicates("article_idx").head(n)


def allocate_proportional(frame: pd.DataFrame, n: int, group_col: str) -> dict[str, int]:
    counts = frame[group_col].value_counts().to_dict()
    raw = {key: n * count / max(len(frame), 1) for key, count in counts.items()}
    quotas = {key: int(value) for key, value in raw.items()}
    remainder = n - sum(quotas.values())
    order = sorted(raw, key=lambda key: raw[key] - quotas[key], reverse=True)
    for key in order[:remainder]:
        quotas[key] += 1
    return quotas


def stratified_validation(frame: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    work = frame.copy()
    work["validation_stratum"] = (
        work["list_alignment_status"].fillna("UNKNOWN").astype(str)
        + "|"
        + work["gold_claim_class"].fillna("UNKNOWN").astype(str)
        + "|"
        + work["gold_source_scope"].fillna("UNKNOWN").astype(str)
    )
    # First take one row from each stratum in shuffled order, then fill the remainder.
    first = []
    for offset, (_, group) in enumerate(work.groupby("validation_stratum", sort=True)):
        group = group[~group["article_idx"].astype(int).isin(
            set(pd.concat(first, ignore_index=False)["article_idx"].astype(int)) if first else set()
        )]
        picked = choose_one_per_article(group, 1, seed + offset)
        if not picked.empty:
            first.append(picked)
    selected = pd.concat(first, ignore_index=False) if first else work.iloc[0:0]
    selected_articles = set(selected["article_idx"].astype(int))
    remaining = work[~work["article_idx"].astype(int).isin(selected_articles)]
    if len(selected) < n:
        selected = pd.concat(
            [selected, choose_one_per_article(remaining, n - len(selected), seed + 10000)],
            ignore_index=False,
        )
    return selected.head(n).drop(columns=["validation_stratum"], errors="ignore")


def final_gold(frame: pd.DataFrame, n: int, min_per_class: int, seed: int) -> pd.DataFrame:
    classes = sorted(frame["gold_claim_class"].fillna("UNKNOWN").unique())
    selected_parts = []
    used_articles: set[int] = set()
    for offset, claim_class in enumerate(classes):
        group = frame[frame["gold_claim_class"].fillna("UNKNOWN") == claim_class]
        group = group[~group["article_idx"].astype(int).isin(used_articles)]
        picked = choose_one_per_article(group, min_per_class, seed + offset)
        selected_parts.append(picked)
        used_articles.update(picked["article_idx"].astype(int))
    selected = pd.concat(selected_parts, ignore_index=False) if selected_parts else frame.iloc[0:0]
    remaining = frame[~frame["article_idx"].astype(int).isin(used_articles)]
    if len(selected) < n:
        selected = pd.concat(
            [selected, choose_one_per_article(remaining, n - len(selected), seed + 20000)],
            ignore_index=False,
        )
    return selected.head(n)


def prepare(frame: pd.DataFrame, kind: str) -> pd.DataFrame:
    result = frame.copy().reset_index(drop=True)
    result.insert(0, "evaluation_id", [f"{kind}_{i:04d}" for i in range(1, len(result) + 1)])
    for column, default in HCX_COLUMNS.items():
        result[column] = default
    for column, default in REVIEW_COLUMNS.items():
        result[column] = default
    result["evaluation_set"] = kind
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--pilot-size", type=int, default=120)
    parser.add_argument("--validation-size", type=int, default=300)
    parser.add_argument("--final-size", type=int, default=500)
    parser.add_argument("--min-per-class", type=int, default=30)
    args = parser.parse_args()

    frame = pd.read_csv(args.input, keep_default_na=False)
    required = {"article_idx", "list_alignment_status", "gold_claim_class", "gold_source_scope"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"input is missing columns: {missing}")

    shuffled = frame.sample(frac=1, random_state=args.seed).reset_index(drop=True)
    pilot = choose_one_per_article(shuffled, args.pilot_size, args.seed)
    used = set(pilot["article_idx"].astype(int))
    remaining = shuffled[~shuffled["article_idx"].astype(int).isin(used)]
    validation = stratified_validation(remaining, args.validation_size, args.seed + 1)
    used.update(validation["article_idx"].astype(int))
    remaining = remaining[~remaining["article_idx"].astype(int).isin(used)]
    final = final_gold(remaining, args.final_size, args.min_per_class, args.seed + 2)

    sets = {
        "pilot120": prepare(pilot, "pilot120"),
        "validation300": prepare(validation, "validation300"),
        "final500": prepare(final, "final500"),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"input": str(args.input.relative_to(ROOT)), "seed": args.seed, "sets": {}}
    for name, result in sets.items():
        path = args.output_dir / f"{name}.csv"
        result.to_csv(path, index=False, encoding="utf-8-sig")
        manifest["sets"][name] = {
            "output": str(path.relative_to(ROOT)),
            "rows": len(result),
            "articles": int(result["article_idx"].nunique()),
            "class_counts": result["gold_claim_class"].value_counts().to_dict(),
            "status_counts": result["list_alignment_status"].value_counts().to_dict(),
            "source_scope_counts": result["gold_source_scope"].value_counts().to_dict(),
        }
        if name == "final500":
            class_counts = result["gold_claim_class"].value_counts().to_dict()
            manifest["sets"][name]["min_per_class_target"] = args.min_per_class
            manifest["sets"][name]["class_shortfalls"] = {
                key: args.min_per_class - value
                for key, value in class_counts.items()
                if value < args.min_per_class
            }
    manifest_path = args.output_dir / "evaluation_sets_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
