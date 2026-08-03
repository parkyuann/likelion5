"""Freeze density-stratified dev/holdout article splits after human judgment."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


SPLIT_SIZES = {"dev": 12, "holdout_1": 12, "holdout_2": 12}
MIN_HIGH_PER_SPLIT = 3
ALLOWED_SCOPES = {
    "KOSIS등재",
    "공식기관_비KOSIS",
    "민간기관",
    "해외기관",
    "불명",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stable_key(row: dict[str, Any], seed: str) -> str:
    return hashlib.sha256(
        f"{seed}:{row['article_idx']}".encode("utf-8")
    ).hexdigest()


def _validate_judgments(frame: pd.DataFrame) -> None:
    required = {
        "article_idx",
        "density_bin",
        "judged_source_scope",
        "judge_note",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"judgment sheet missing columns: {sorted(missing)}")
    blank = frame["judged_source_scope"].fillna("").astype(str).str.strip().eq("")
    if blank.any():
        ids = frame.loc[blank, "article_idx"].astype(str).tolist()
        raise ValueError(f"unjudged rows remain: {ids}")
    invalid = sorted(
        set(frame["judged_source_scope"]) - ALLOWED_SCOPES
    )
    if invalid:
        raise ValueError(f"invalid judged_source_scope: {invalid}")


def _allocate_splits(
    judged_kosis: list[dict[str, Any]],
    clean_reserved: list[dict[str, Any]],
    *,
    seed: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    splits: dict[str, list[dict[str, Any]]] = {
        "dev": [],
        "holdout_1": list(clean_reserved),
        "holdout_2": [],
    }
    if len(clean_reserved) != 2:
        raise ValueError(
            f"expected exactly 2 clean reserved articles, got {len(clean_reserved)}"
        )
    reserved_ids = {row["article_idx"] for row in clean_reserved}
    candidates = [
        row for row in judged_kosis
        if row["article_idx"] not in reserved_ids
    ]
    high_pool = sorted(
        [row for row in candidates if row["density_bin"] == "HIGH"],
        key=lambda row: _stable_key(row, f"{seed}:high"),
    )
    for split_name in ("dev", "holdout_1", "holdout_2"):
        existing_high = sum(
            row["density_bin"] == "HIGH" for row in splits[split_name]
        )
        required = max(0, MIN_HIGH_PER_SPLIT - existing_high)
        if len(high_pool) < required:
            raise ValueError(
                "insufficient HIGH articles for minimum 3 per split: "
                f"failed at {split_name}, need {required}, have {len(high_pool)}"
            )
        splits[split_name].extend(high_pool[:required])
        del high_pool[:required]

    assigned_ids = {
        row["article_idx"]
        for rows in splits.values()
        for row in rows
    }
    remaining = [
        row for row in candidates
        if row["article_idx"] not in assigned_ids
    ]
    remaining = sorted(
        remaining,
        key=lambda row: (
            {"LOW": 0, "MID": 1, "HIGH": 2}.get(row["density_bin"], 9),
            _stable_key(row, f"{seed}:fill"),
        ),
    )
    while any(len(splits[name]) < SPLIT_SIZES[name] for name in splits):
        if not remaining:
            raise ValueError(
                "fewer than 36 KOSIS articles available after judgment"
            )
        row = remaining.pop(0)
        available = [
            name for name in ("dev", "holdout_1", "holdout_2")
            if len(splits[name]) < SPLIT_SIZES[name]
        ]
        target = min(
            available,
            key=lambda name: (
                sum(
                    item["density_bin"] == row["density_bin"]
                    for item in splits[name]
                ),
                len(splits[name]) / SPLIT_SIZES[name],
                name,
            ),
        )
        splits[target].append(row)

    for name, rows in splits.items():
        high_count = sum(row["density_bin"] == "HIGH" for row in rows)
        if high_count < MIN_HIGH_PER_SPLIT:
            raise ValueError(
                f"{name} HIGH count {high_count} < {MIN_HIGH_PER_SPLIT}"
            )
    reserve = remaining
    return splits, reserve


def _read_judgment_sheet(path: Path) -> pd.DataFrame:
    if path.suffix.casefold() == ".xlsx":
        return pd.read_excel(
            path,
            sheet_name="판정입력",
            header=3,
            dtype=str,
            keep_default_na=False,
        )
    return pd.read_csv(
        path,
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False,
    )


def freeze_stratum_splits(
    judgment_sheet: Path,
    candidate_internal_path: Path,
    clean_reserved_path: Path,
    output_root: Path,
    *,
    seed: str = "r17-stratum-freeze-20260730",
) -> dict[str, Any]:
    frame = _read_judgment_sheet(judgment_sheet)
    _validate_judgments(frame)
    internal = {
        str(row["article_idx"]): row
        for row in _read_jsonl(candidate_internal_path)
    }
    clean_reserved = _read_jsonl(clean_reserved_path)
    judged_kosis = []
    for record in frame.to_dict(orient="records"):
        if record["judged_source_scope"] != "KOSIS등재":
            continue
        article_idx = str(record["article_idx"])
        source = internal.get(article_idx)
        if source is None:
            raise ValueError(
                f"judged article missing internal source: {article_idx}"
            )
        if source["density_bin"] != record["density_bin"]:
            raise ValueError(
                f"density mismatch for article {article_idx}"
            )
        judged_kosis.append(source)

    splits, reserve = _allocate_splits(
        judged_kosis,
        clean_reserved,
        seed=seed,
    )
    created_at = datetime.now(timezone.utc).isoformat()
    output_root.mkdir(parents=True, exist_ok=True)
    directory_map = {
        "dev": output_root / "dev",
        "holdout_1": output_root / "holdout_1",
        "holdout_2": output_root / "sealed_holdout_2",
    }
    manifests = {}
    for split_name, rows in splits.items():
        target_dir = directory_map[split_name]
        target_dir.mkdir(parents=True, exist_ok=True)
        article_rows = [{
            "article_idx": row["article_idx"],
            "title": row["기사제목"],
            "date": row["작성일"],
            "article_sha256": row["article_sha256"],
            "density_bin": row["density_bin"],
            "article_text": row["article_text"],
        } for row in rows]
        (target_dir / "input.jsonl").write_text(
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n"
                for row in article_rows
            ),
            encoding="utf-8",
        )
        manifest = {
            "contract_version": "l_stratum_frozen_split_v1",
            "split": split_name,
            "sealed": split_name == "holdout_2",
            "created_at_utc": created_at,
            "seed": seed,
            "article_count": len(rows),
            "density_distribution": dict(Counter(
                row["density_bin"] for row in rows
            )),
            "articles": [{
                "article_idx": row["article_idx"],
                "article_sha256": row["article_sha256"],
                "density_bin": row["density_bin"],
            } for row in rows],
        }
        (target_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifests[split_name] = manifest
    public_manifest = {
        "contract_version": "l_stratum_freeze_v1",
        "created_at_utc": created_at,
        "judged_rows": len(frame),
        "judged_kosis_rows": len(judged_kosis),
        "clean_reserved_rows": len(clean_reserved),
        "reserve_kosis_rows": len(reserve),
        "splits": {
            name: {
                "article_count": manifest["article_count"],
                "density_distribution": manifest["density_distribution"],
                "manifest_sha256": hashlib.sha256(
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for name, manifest in manifests.items()
        },
        "holdout_2_default_readable": False,
        "holdout_2_path": "sealed_holdout_2",
    }
    (output_root / "freeze_manifest.json").write_text(
        json.dumps(public_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return public_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--judgment-sheet",
        "--judgment-csv",
        dest="judgment_sheet",
        type=Path,
        required=True,
    )
    parser.add_argument("--candidate-internal", type=Path, required=True)
    parser.add_argument("--clean-reserved", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    report = freeze_stratum_splits(
        args.judgment_sheet,
        args.candidate_internal,
        args.clean_reserved,
        args.output_root,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
