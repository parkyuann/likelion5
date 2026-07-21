"""기존 평가셋 gold 컬럼을 신 스키마로 in-place 정렬한다(재샘플링 없음).

E 단계: silver 라벨러를 신 어휘로 고쳤으나, 기존 pilot120/validation300/final500의
행 구성은 실험 비교를 위해 보존해야 한다. 그래서 라벨러를 다시 돌려 재샘플링하는 대신,
이미 뽑힌 평가셋의 gold 컬럼만 제자리에서 변환한다.

변환:
  - gold_claim_class == "노이즈"  →  claim_class="", is_claim=False, noise_reason="기타"
  - 그 외(10종)                    →  is_claim=True, noise_reason=""
  - gold_is_claim / gold_noise_reason 컬럼 신설
  - gold_verifiability_prefilter = compute_verifiability_prefilter(...) (3값)
  - gold_source_scope는 손대지 않는다(pred와 독립 유지, 순환 회피)

원본은 <name>.pre_v2.csv 백업으로 남긴다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieval_schema import compute_verifiability_prefilter

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "data" / "evaluation"
TARGETS = ["pilot120.csv", "validation300.csv", "final500.csv"]


def migrate(path: Path) -> dict:
    df = pd.read_csv(path, keep_default_na=False)
    if "gold_claim_class" not in df.columns:
        return {"file": path.name, "skipped": "no gold_claim_class"}

    old_class = df["gold_claim_class"].astype(str)
    is_noise = old_class == "노이즈"
    new_class = old_class.where(~is_noise, "")
    is_claim = new_class != ""
    noise_reason = is_noise.map(lambda x: "기타" if x else "")
    scope = df["gold_source_scope"].astype(str) if "gold_source_scope" in df.columns else ""

    prefilter = [
        compute_verifiability_prefilter(bool(ic), cc, sc)[0]
        for ic, cc, sc in zip(is_claim, new_class, scope if len(scope) else [""] * len(df))
    ]

    df["gold_is_claim"] = is_claim
    df["gold_claim_class"] = new_class
    df["gold_noise_reason"] = noise_reason
    df["gold_verifiability_prefilter"] = prefilter

    backup = path.with_suffix(".pre_v2.csv")
    if not backup.exists():
        pd.read_csv(path, keep_default_na=False).to_csv(backup, index=False, encoding="utf-8-sig")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return {
        "file": path.name,
        "rows": len(df),
        "noise_remapped": int(is_noise.sum()),
        "gold_is_claim_true": int(is_claim.sum()),
        "prefilter_dist": pd.Series(prefilter).value_counts().to_dict(),
    }


def main() -> None:
    for name in TARGETS:
        path = EVAL_DIR / name
        if not path.exists():
            print({"file": name, "skipped": "missing"})
            continue
        print(migrate(path))


if __name__ == "__main__":
    main()
