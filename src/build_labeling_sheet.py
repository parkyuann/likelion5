"""F단계 gold + 추출 리콜 audit 공유 라벨링 시트 빌더 (실전1, Phase 0).

한 번의 사람 라벨링 패스로 두 가지 truth를 동시에 확보한다.

  1) claim_class gold  — 추출된 claim에 사람이 진짜 claim_class를 부여(F단계 macro-F1의 정답).
  2) 추출 리콜 audit   — 추출기가 '숫자가 있는데 버린' 문장을 사람이
                          '추출됐어야 할 실제 통계주장인가'로 판정(claim_extractor 방법론 수정 근거).

두 태스크는 **같은 표본 기사**를 공유한다. gold 표본은 실버 claim_class로 층화(coverage 확보용;
실버는 정답이 아니라 '어느 문장을 사람에게 보여줄지' 고르는 용도로만 쓴다). audit 문장은 그
기사들의 원문에서 현재 추출기가 버린 숫자 문장을 그대로 나열한다.

추출기는 **손대지 않는다** — 깨끗한 리콜 baseline을 위해. 시트가 라벨링되면 그 결과로
claim_extractor.py를 표적 수정(3단계)하고, 같은 audit 문장 프레임에 새 추출기를 다시 돌려
리콜을 재측정한다(라벨 재작업 불필요).

실행:
    venv/Scripts/python.exe src/build_labeling_sheet.py
    venv/Scripts/python.exe src/build_labeling_sheet.py --per-class 25 --seed 20260722
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claim_extractor import extract_from_sentence, iter_sentence_spans  # noqa: E402
from retrieval_schema import CLAIM_CLASSES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POOL = ROOT / "data" / "retrieval_eval_claims_v0_codex.csv"
DEFAULT_NEWS = ROOT / "data" / "news_preprocessed.csv"
DEFAULT_OUT_DIR = ROOT / "data" / "labeling"
SEED = 20260722

DIGIT_RE = re.compile(r"\d")

# 사람이 고를 수 있는 claim_class는 스키마 10종 전체 + 노이즈(is_claim=False) 표식.
# 실버 라벨러가 못 뱉는 3종(사고대응임시통계/여론조사/정정보도)도 열어둬야 코퍼스에 실재하는지
# 이 라벨링에서 비로소 확인된다.
ALLOWED_CLASS_VOCAB = sorted(CLAIM_CLASSES) + ["노이즈(=is_claim False)"]

# audit 문장 누락 유형(사람이 선택; 추출기 수정 우선순위 산출용)
MISS_TYPE_VOCAB = ["무단위비율지수", "OOV단위", "한글수", "무값정성주장", "비주장노이즈", "기타"]


def stratified_claim_gold(pool: pd.DataFrame, per_class: int, seed: int) -> pd.DataFrame:
    """실버 gold_claim_class로 층화해 클래스당 최대 per_class개, 기사 1개당 1행으로 뽑는다."""
    work = pool.copy()
    work["_class"] = work["gold_claim_class"].replace("", "노이즈").fillna("노이즈").astype(str)
    picked_parts: list[pd.DataFrame] = []
    used_articles: set[int] = set()
    for offset, cls in enumerate(sorted(work["_class"].unique())):
        group = work[work["_class"] == cls]
        group = group[~group["article_idx"].astype(int).isin(used_articles)]
        # 기사 1개당 1행: 섞은 뒤 article_idx 중복 제거
        shuffled = group.sample(frac=1, random_state=seed + offset)
        chosen = shuffled.drop_duplicates("article_idx").head(per_class)
        picked_parts.append(chosen)
        used_articles.update(chosen["article_idx"].astype(int))
    return pd.concat(picked_parts, ignore_index=True) if picked_parts else work.iloc[0:0]


def dropped_digit_sentences(article_idx: int, news: pd.DataFrame) -> list[dict]:
    """기사 원문에서 '숫자는 있으나 현재 추출기가 버린' 문장을 반환(추출 audit 대상)."""
    text = news.iloc[article_idx].get("본문_정제")
    if not isinstance(text, str) or not text:
        return []
    dropped: list[dict] = []
    for sentence_index, char_start, char_end, sent in iter_sentence_spans(text):
        if not DIGIT_RE.search(sent):
            continue
        if extract_from_sentence(sent) is not None:
            continue  # 추출된 문장 → 누락 아님
        dropped.append({
            "claim_text": sent,
            "sentence_index": sentence_index,
            "sentence_char_start": char_start,
            "sentence_char_end": char_end,
        })
    return dropped


def all_extracted_sentences(pool: pd.DataFrame, article_ids: set[int]) -> pd.DataFrame:
    """Return one row per extracted sentence in the sampled articles.

    Recall is sentence-level because the audit frame is sentence-level.  List-form
    post-processing may create several rows for one sentence, so those rows must be
    collapsed before annotation and before constructing the recall denominator.
    """
    work = pool[pool["article_idx"].astype(int).isin(article_ids)].copy()
    work["_claim_key"] = work["claim_text"].fillna("").astype(str).str.strip()
    work = work[work["_claim_key"] != ""]
    return (
        work.sort_values(["article_idx", "_claim_key"])
        .drop_duplicates(["article_idx", "_claim_key"], keep="first")
        .drop(columns=["_claim_key"])
        .reset_index(drop=True)
    )


def build(
    pool: pd.DataFrame,
    news: pd.DataFrame,
    per_class: int,
    seed: int,
    include_all_extracted: bool = True,
) -> tuple[pd.DataFrame, dict]:
    representatives = stratified_claim_gold(pool, per_class, seed)
    audit_articles = sorted(set(representatives["article_idx"].astype(int)))
    gold = (
        all_extracted_sentences(pool, set(audit_articles))
        if include_all_extracted
        else representatives
    )

    rows: list[dict] = []
    rid = 0

    # (1) claim_class gold 태스크 — 추출된 claim 1건에 사람이 진짜 class를 매김
    for _, r in gold.iterrows():
        rid += 1
        rows.append({
            "row_id": f"L{rid:05d}",
            "task": "claim_class",
            "article_idx": int(r["article_idx"]),
            "기사제목": r.get("기사제목", ""),
            "작성일": r.get("작성일", ""),
            "claim_text": r.get("claim_text", ""),
            "value_list": r.get("value_list", ""),
            "unit_list": r.get("unit_list", ""),
            "change_type": r.get("change_type", ""),
            "sentence_index": r.get("sentence_index", ""),
            "sentence_char_start": r.get("sentence_char_start", ""),
            "sentence_char_end": r.get("sentence_char_end", ""),
            # --- 사람 입력 칸 ---
            "human_is_claim": "",          # Y / N
            "human_claim_class": "",       # ALLOWED_CLASS_VOCAB 중 하나 (is_claim=Y일 때)
            "human_miss_type": "",         # (claim_class 태스크에선 공란)
            "human_notes": "",
            # --- 참고(판단 후 대조용, 라벨 시 보지 말 것) ---
            "zz_silver_claim_class_ref": r.get("gold_claim_class", ""),
        })

    # (2) 추출 audit 태스크 — 위 기사들의 '버려진 숫자 문장'을 사람이 판정
    for ai in audit_articles:
        for dropped in dropped_digit_sentences(ai, news):
            rid += 1
            rows.append({
                "row_id": f"L{rid:05d}",
                "task": "extraction_audit",
                "article_idx": ai,
                "기사제목": news.iloc[ai].get("기사제목", ""),
                "작성일": news.iloc[ai].get("작성일", ""),
                "claim_text": dropped["claim_text"],
                "value_list": "",
                "unit_list": "",
                "change_type": "",
                "sentence_index": dropped["sentence_index"],
                "sentence_char_start": dropped["sentence_char_start"],
                "sentence_char_end": dropped["sentence_char_end"],
                # --- 사람 입력 칸 ---
                "human_is_claim": "",          # Y=추출됐어야 할 실제 통계주장 / N=아님
                "human_claim_class": "",       # is_claim=Y면 어떤 class였을지
                "human_miss_type": "",         # MISS_TYPE_VOCAB 중 하나 (is_claim=Y일 때)
                "human_notes": "",
                "zz_silver_claim_class_ref": "",
            })

    sheet = pd.DataFrame(rows)
    manifest = {
        "seed": seed,
        "per_class": per_class,
        "allowed_class_vocab": ALLOWED_CLASS_VOCAB,
        "miss_type_vocab": MISS_TYPE_VOCAB,
        "articles": len(audit_articles),
        "rows_total": len(sheet),
        "rows_claim_class": int((sheet["task"] == "claim_class").sum()),
        "rows_extraction_audit": int((sheet["task"] == "extraction_audit").sum()),
        "claim_class_silver_distribution": gold["gold_claim_class"].replace("", "노이즈").value_counts().to_dict(),
        "selection_representatives": len(representatives),
        "claim_rows_are_all_unique_extracted_sentences": include_all_extracted,
        "recall_denominator_identifiable_after_labeling": include_all_extracted,
    }
    return sheet, manifest


INSTRUCTIONS = """# 라벨링 지침 (F-gold + 추출 audit 공유 시트)

한 시트에 두 종류 행이 섞여 있다(`task` 열로 구분). 각 행의 `claim_text`만 보고 판단한다.

## 공통 어휘
- claim_class(10종): 집계통계 · 개별사례 · 전망예측 · 목표계획 · 법령제도 · 해석수사 ·
  사고대응임시통계 · 여론조사 · 통계조사안내 · 정정보도. 주장이 아니면 `노이즈(=is_claim False)`.

## task = claim_class  (추출된 문장의 진짜 class 확정 → F단계 정답)
1. `human_is_claim`: 검증 가능한 통계적 주장이면 `Y`, 광고·의견·UI잡음 등이면 `N`.
2. `human_is_claim=Y`면 `human_claim_class`에 위 10종 중 하나. `N`이면 `노이즈(=is_claim False)`.
3. `zz_silver_claim_class_ref`는 기계(규칙) 추정값이다. **판단이 끝난 뒤에만** 참고하고,
   처음부터 보고 따라가지 말 것(편향 방지).

## task = extraction_audit  (추출기가 버린 숫자 문장 → 방법론 수정 근거)
1. `human_is_claim`: 이 문장이 **추출됐어야 할 실제 통계 주장**이면 `Y`, 아니면(날짜·서수·
   고유명사·비통계 수치) `N`.
2. `Y`면 `human_claim_class`에 해당 class, `human_miss_type`에 왜 규칙이 놓쳤는지:
   무단위비율지수 · OOV단위 · 한글수 · 무값정성주장 · 비주장노이즈 · 기타.

## 산출
- 라벨 완료 후 이 시트로 (a) claim_class per-class F1·macro-F1, (b) 추출 진짜 리콜 손실률과
  누락 유형 분포를 계산한다. 누락 유형 상위가 claim_extractor.py 수정 우선순위가 된다.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--news", type=Path, default=DEFAULT_NEWS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--per-class", type=int, default=25, help="claim_class 태스크 클래스당 최대 표본")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--representatives-only",
        action="store_true",
        help="legacy behavior: one extracted claim per sampled article; overall recall cannot be computed",
    )
    parser.add_argument("--output-name", default="labeling_sheet_v2.csv")
    args = parser.parse_args()

    pool = pd.read_csv(args.pool, keep_default_na=False)
    news = pd.read_csv(args.news, keep_default_na=False)
    required = {"article_idx", "claim_text", "gold_claim_class"}
    missing = sorted(required - set(pool.columns))
    if missing:
        raise ValueError(f"pool is missing columns: {missing}")

    sheet, manifest = build(
        pool,
        news,
        args.per_class,
        args.seed,
        include_all_extracted=not args.representatives_only,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    sheet_path = args.out_dir / args.output_name
    manifest_path = args.out_dir / f"{Path(args.output_name).stem}_manifest.json"
    readme_path = args.out_dir / "labeling_instructions.md"
    sheet.to_csv(sheet_path, index=False, encoding="utf-8-sig")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    readme_path.write_text(INSTRUCTIONS, encoding="utf-8")

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nsheet   -> {sheet_path}")
    print(f"manifest-> {manifest_path}")
    print(f"readme  -> {readme_path}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
