"""HCX 주장 추출 평가용 층화 라벨링 표본을 생성한다.

두 종류의 표본을 함께 만든다.

1. 후보 문장 표본
   ``claim_candidates_relaxed.csv``에서 기존 필터와 완화 필터의 공통 후보
   (``both``)와 완화로 새로 들어온 후보(``relaxed_only``)를 반씩 뽑는다.
2. 기사 전체 표본
   ``news_preprocessed.csv``에서 뽑으며, 정규식 후보 밖의 주장까지 사람이 전부
   라벨링해 end-to-end 주장 탐지 Recall을 측정하는 용도다.

검색 구분 레이블(True/False)은 목표 비율을 먼저 고정하고, 각 레이블 안에서
여러 층을 round-robin으로 순회한다. 자연 분포 그대로 뽑으면 False 기사가 너무
적어 오류 분석이 어려우므로 기본값은 False 25%다. 이 표본은 모집단 비율 추정용이
아니라 모델 비교·오류 분석용 평가셋이라는 점에 유의한다.

사용 예 (레포 루트):
    venv/Scripts/python.exe src/create_labeling_samples.py
    venv/Scripts/python.exe src/create_labeling_samples.py \
        --candidate-size 100 --article-size 50 --seed 20260713
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CANDIDATES = ROOT / "data" / "claim_candidates_relaxed.csv"
DEFAULT_ARTICLES = ROOT / "data" / "news_preprocessed.csv"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "labeling"

LABEL_COLUMN = "검색_구분_레이블"
ARTICLE_LABEL_COLUMN = "검색 구분 레이블"

TOPIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("노동", re.compile(r"고용|취업|실업|근로|노동|임금|비정규직|일자리")),
    ("물가", re.compile(r"물가|가격|쌀값|소비자물가|생산자물가")),
    ("인구", re.compile(r"인구|출생|사망|혼인|이혼|고령|청년|가구")),
    ("주거", re.compile(r"주택|아파트|부동산|전세|월세|매매가격")),
    ("무역", re.compile(r"수출|수입|무역|관세|경상수지")),
    ("보건복지", re.compile(r"보건|의료|건강|질병|복지|연금|빈곤")),
    ("농림수산", re.compile(r"농가|농업|농림|쌀|과수|어가|수산|어업")),
    ("금융", re.compile(r"금리|은행|대출|주가|증시|채권|가상자산|비트코인")),
    ("산업경기", re.compile(r"생산|산업|기업|성장률|GDP|경기|매출")),
]

CANDIDATE_GOLD_COLUMNS = [
    # 주장 탐지 정답과 KOSIS 검증 가능성 정답을 섞지 않는다.
    "gold_is_aggregate_claim",
    "gold_claim_class",
    "gold_source_scope",
    "gold_verifiability_prefilter",
    "gold_indicator_raw",
    "gold_population",
    "gold_value",
    "gold_unit",
    "gold_time_ref",
    "gold_time_compare",
    "gold_source_org_raw",
    "gold_source_role",
    "gold_source_evidence_quote",
    "gold_notes",
    "annotator",
]

ARTICLE_GOLD_COLUMNS = [
    "gold_annotation_complete",
    "gold_claim_count",
    "gold_claims_json",
    "gold_notes",
    "annotator",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HCX 평가용 층화 라벨링 표본 생성")
    parser.add_argument("--candidate-input", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--article-input", type=Path, default=DEFAULT_ARTICLES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--sample-name",
        default="pilot_relaxed",
        help="출력 파일명에 사용할 세트 이름",
    )
    parser.add_argument(
        "--exclude-candidate-sample",
        type=Path,
        help="candidate_row_id가 겹치지 않도록 제외할 기존 후보 라벨링 CSV",
    )
    parser.add_argument(
        "--exclude-article-sample",
        type=Path,
        help="article_idx가 겹치지 않도록 제외할 기존 기사 라벨링 CSV",
    )
    parser.add_argument("--candidate-size", type=int, default=50)
    parser.add_argument("--article-size", type=int, default=30)
    parser.add_argument("--false-share", type=float, default=0.25)
    parser.add_argument(
        "--relaxed-only-share",
        type=float,
        default=0.50,
        help="후보 문장 표본에서 relaxed_only가 차지할 목표 비율",
    )
    parser.add_argument("--seed", type=int, default=20260713)
    return parser.parse_args()


def normalize_bool(value) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def classify_topic(title: str) -> str:
    text = str(title or "")
    for topic, pattern in TOPIC_PATTERNS:
        if pattern.search(text):
            return topic
    return "기타"


def value_count_bucket(value) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 0
    if count <= 1:
        return "단일값"
    if count == 2:
        return "2개값"
    return "3개이상"


def candidate_density_bucket(count: int) -> str:
    if count == 0:
        return "후보없음"
    if count <= 2:
        return "후보1-2"
    if count <= 5:
        return "후보3-5"
    return "후보6이상"


def _round_robin_sample(
    frame: pd.DataFrame,
    size: int,
    strata: list[str],
    rng: np.random.Generator,
) -> pd.DataFrame:
    """각 복합 층에서 한 행씩 순환 선택해 희소 오류 유형도 포함한다."""
    if size <= 0 or frame.empty:
        return frame.iloc[0:0].copy()
    size = min(size, len(frame))

    queues: list[deque[int]] = []
    group_items = list(frame.groupby(strata, dropna=False, sort=True).groups.items())
    rng.shuffle(group_items)
    for _, indices in group_items:
        shuffled = np.asarray(list(indices), dtype=int)
        rng.shuffle(shuffled)
        queues.append(deque(int(i) for i in shuffled))

    selected: list[int] = []
    while queues and len(selected) < size:
        next_round: list[deque[int]] = []
        for queue in queues:
            if len(selected) >= size:
                break
            selected.append(queue.popleft())
            if queue:
                next_round.append(queue)
        queues = next_round
        rng.shuffle(queues)

    return frame.loc[selected].copy()


def stratified_binary_label_sample(
    frame: pd.DataFrame,
    size: int,
    false_share: float,
    strata: list[str],
    seed: int,
) -> pd.DataFrame:
    """True/False 할당량을 지키고 각 레이블 내부를 복합 층화한다."""
    if not 0 <= false_share <= 1:
        raise ValueError("--false-share는 0과 1 사이여야 합니다.")
    if size < 0:
        raise ValueError("표본 크기는 0 이상이어야 합니다.")
    if size > len(frame):
        raise ValueError(f"요청 표본 {size}건이 전체 {len(frame)}건보다 큽니다.")

    false_pool = frame.loc[~frame["label_bool"]]
    true_pool = frame.loc[frame["label_bool"]]
    # Python round()의 banker's rounding(12.5 -> 12)을 피하고 일반적인 반올림을 쓴다.
    false_target = min(int(np.floor(size * false_share + 0.5)), len(false_pool))
    true_target = min(size - false_target, len(true_pool))

    # 한쪽 풀이 부족하면 다른 쪽에서 남은 수량을 채운다.
    remaining = size - false_target - true_target
    if remaining:
        false_extra = min(remaining, len(false_pool) - false_target)
        false_target += false_extra
        remaining -= false_extra
    if remaining:
        true_extra = min(remaining, len(true_pool) - true_target)
        true_target += true_extra
        remaining -= true_extra
    if remaining:
        raise ValueError("요청한 표본 수를 채울 수 없습니다.")

    false_rng = np.random.default_rng(seed)
    true_rng = np.random.default_rng(seed + 1)
    sampled = pd.concat(
        [
            _round_robin_sample(false_pool, false_target, strata, false_rng),
            _round_robin_sample(true_pool, true_target, strata, true_rng),
        ],
        ignore_index=False,
    )
    order_rng = np.random.default_rng(seed + 2)
    order = order_rng.permutation(len(sampled))
    return sampled.iloc[order].reset_index(drop=True)


def stratified_candidate_origin_sample(
    frame: pd.DataFrame,
    size: int,
    relaxed_only_share: float,
    false_share: float,
    seed: int,
) -> pd.DataFrame:
    """both/relaxed_only 할당량을 고정한 뒤 각 집합 안에서 레이블·유형을 층화한다."""
    if not 0 <= relaxed_only_share <= 1:
        raise ValueError("--relaxed-only-share는 0과 1 사이여야 합니다.")
    if size > len(frame):
        raise ValueError(f"요청 표본 {size}건이 전체 {len(frame)}건보다 큽니다.")

    relaxed_pool = frame.loc[frame["candidate_origin"] == "relaxed_only"]
    both_pool = frame.loc[frame["candidate_origin"] == "both"]
    relaxed_target = min(int(np.floor(size * relaxed_only_share + 0.5)), len(relaxed_pool))
    both_target = min(size - relaxed_target, len(both_pool))

    remaining = size - relaxed_target - both_target
    if remaining:
        relaxed_extra = min(remaining, len(relaxed_pool) - relaxed_target)
        relaxed_target += relaxed_extra
        remaining -= relaxed_extra
    if remaining:
        both_extra = min(remaining, len(both_pool) - both_target)
        both_target += both_extra
        remaining -= both_extra
    if remaining:
        raise ValueError("both/relaxed_only 후보가 부족해 요청한 표본 수를 채울 수 없습니다.")

    # 검색 레이블은 각 origin 안에서 할당하고, 사용자가 요청한 핵심 층인
    # change_type/value_count를 round-robin으로 순회한다.
    relaxed_sample = stratified_binary_label_sample(
        relaxed_pool,
        relaxed_target,
        false_share,
        strata=["change_type", "sampling_value_count"],
        seed=seed,
    )
    both_sample = stratified_binary_label_sample(
        both_pool,
        both_target,
        false_share,
        strata=["change_type", "sampling_value_count"],
        seed=seed + 10,
    )
    sampled = pd.concat([relaxed_sample, both_sample], ignore_index=True)
    rng = np.random.default_rng(seed + 20)
    return sampled.iloc[rng.permutation(len(sampled))].reset_index(drop=True)


def prepare_candidates(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "article_idx", "기사제목", LABEL_COLUMN, "change_type", "value_count",
        "source_mentioned", "claim_text", "passes_old_filter",
        "passes_relaxed_filter", "candidate_origin",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"후보 파일 필수 컬럼 누락: {sorted(missing)}")

    frame = frame.copy()
    frame["label_bool"] = frame[LABEL_COLUMN].map(normalize_bool)
    frame["sampling_topic"] = frame["기사제목"].map(classify_topic)
    frame["sampling_value_count"] = frame["value_count"].map(value_count_bucket)
    frame["sampling_source"] = frame["source_mentioned"].map(normalize_bool).map(
        {True: "출처패턴있음", False: "출처패턴없음"}
    )
    frame["candidate_row_id"] = frame.index
    return frame


def prepare_articles(article_path: Path, candidate_path: Path) -> pd.DataFrame:
    articles = pd.read_csv(article_path)
    required = {"기사제목", "작성일", "본문_정제", ARTICLE_LABEL_COLUMN}
    missing = required - set(articles.columns)
    if missing:
        raise ValueError(f"기사 파일 필수 컬럼 누락: {sorted(missing)}")

    candidates = pd.read_csv(candidate_path, usecols=["article_idx", "source_mentioned"])
    candidates["source_signal"] = candidates["source_mentioned"].map(normalize_bool)
    counts = candidates.groupby("article_idx").agg(
        regex_candidate_count=("article_idx", "size"),
        regex_source_signal_count=("source_signal", "sum"),
    )

    articles = articles.copy()
    articles["article_idx"] = articles.index
    articles = articles.join(counts, on="article_idx")
    articles["regex_candidate_count"] = articles["regex_candidate_count"].fillna(0).astype(int)
    articles["regex_source_signal_count"] = articles["regex_source_signal_count"].fillna(0).astype(int)
    articles["label_bool"] = articles[ARTICLE_LABEL_COLUMN].map(normalize_bool)
    articles["sampling_topic"] = articles["기사제목"].map(classify_topic)
    articles["sampling_candidate_density"] = articles["regex_candidate_count"].map(candidate_density_bucket)
    articles["sampling_source"] = (articles["regex_source_signal_count"] > 0).map(
        {True: "출처패턴있음", False: "출처패턴없음"}
    )
    return articles


def add_empty_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        result[column] = ""
    return result


def exclude_existing_sample(
    pool: pd.DataFrame,
    sample_path: Path | None,
    id_column: str,
) -> tuple[pd.DataFrame, int]:
    """기존 라벨링 파일의 ID를 제외해 팀원 간 중복 표본을 방지한다."""
    if sample_path is None:
        return pool, 0
    if not sample_path.is_file():
        raise FileNotFoundError(f"제외 표본 파일을 찾을 수 없습니다: {sample_path}")
    existing = pd.read_csv(sample_path, usecols=[id_column])
    excluded_ids = set(existing[id_column].dropna().astype(int))
    filtered = pool.loc[~pool[id_column].astype(int).isin(excluded_ids)].copy()
    return filtered, len(pool) - len(filtered)


def distribution(frame: pd.DataFrame, columns: list[str]) -> dict:
    result: dict[str, dict[str, int]] = {}
    for column in columns:
        counts = frame[column].astype(str).value_counts(dropna=False).sort_index()
        result[column] = {key: int(value) for key, value in counts.items()}
    return result


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    candidate_pool = prepare_candidates(args.candidate_input)
    candidate_pool, excluded_candidate_count = exclude_existing_sample(
        candidate_pool, args.exclude_candidate_sample, "candidate_row_id"
    )
    candidate_sample = stratified_candidate_origin_sample(
        candidate_pool,
        size=args.candidate_size,
        relaxed_only_share=args.relaxed_only_share,
        false_share=args.false_share,
        seed=args.seed,
    )
    candidate_sample.insert(0, "sample_id", [f"C{i:03d}" for i in range(1, len(candidate_sample) + 1)])
    candidate_sample = add_empty_columns(candidate_sample, CANDIDATE_GOLD_COLUMNS)

    article_pool = prepare_articles(args.article_input, args.candidate_input)
    article_pool, excluded_article_count = exclude_existing_sample(
        article_pool, args.exclude_article_sample, "article_idx"
    )
    article_sample = stratified_binary_label_sample(
        article_pool,
        size=args.article_size,
        false_share=args.false_share,
        strata=["sampling_topic", "sampling_candidate_density", "sampling_source"],
        seed=args.seed + 10,
    )
    # 라벨링에는 정제 본문과 식별·층화 정보만 필요하다. 15KB 안팎의 원본 크롤링
    # 본문(메뉴·광고 포함)을 중복 저장하지 않아 CSV를 사람이 다루기 쉽게 만든다.
    article_columns = [
        "article_idx", "기사제목", "작성일", "URL", ARTICLE_LABEL_COLUMN,
        "섹션", "본문_상태", "본문_정제", "regex_candidate_count",
        "regex_source_signal_count", "label_bool", "sampling_topic",
        "sampling_candidate_density", "sampling_source",
    ]
    article_sample = article_sample[[column for column in article_columns if column in article_sample.columns]]
    article_sample.insert(0, "sample_id", [f"A{i:03d}" for i in range(1, len(article_sample) + 1)])
    article_sample = add_empty_columns(article_sample, ARTICLE_GOLD_COLUMNS)

    candidate_output = args.output_dir / f"candidate_labeling_{args.sample_name}.csv"
    article_output = args.output_dir / f"article_labeling_{args.sample_name}.csv"
    manifest_output = args.output_dir / f"sampling_manifest_{args.sample_name}.json"

    candidate_sample.drop(columns=["label_bool"]).to_csv(candidate_output, index=False, encoding="utf-8-sig")
    article_sample.drop(columns=["label_bool"]).to_csv(article_output, index=False, encoding="utf-8-sig")

    manifest = {
        "seed": args.seed,
        "sample_name": args.sample_name,
        "false_share_target": args.false_share,
        "relaxed_only_share_target": args.relaxed_only_share,
        "exclusions": {
            "candidate_sample": str(args.exclude_candidate_sample.resolve()) if args.exclude_candidate_sample else None,
            "excluded_candidate_count": excluded_candidate_count,
            "article_sample": str(args.exclude_article_sample.resolve()) if args.exclude_article_sample else None,
            "excluded_article_count": excluded_article_count,
        },
        "inputs": {
            "candidates": str(args.candidate_input.resolve()),
            "articles": str(args.article_input.resolve()),
        },
        "candidate_sample": {
            "size": len(candidate_sample),
            "output": str(candidate_output.resolve()),
            "strata": ["candidate_origin", "검색_구분_레이블", "change_type", "sampling_value_count"],
            "distribution": distribution(
                candidate_sample,
                ["candidate_origin", LABEL_COLUMN, "change_type", "sampling_value_count"],
            ),
        },
        "article_sample": {
            "size": len(article_sample),
            "output": str(article_output.resolve()),
            "strata": ["검색 구분 레이블", "sampling_topic", "sampling_candidate_density", "sampling_source"],
            "distribution": distribution(
                article_sample,
                [ARTICLE_LABEL_COLUMN, "sampling_topic", "sampling_candidate_density", "sampling_source"],
            ),
        },
    }
    manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"후보 문장 표본: {len(candidate_sample)}건 -> {candidate_output}")
    print(f"기사 전체 표본: {len(article_sample)}건 -> {article_output}")
    print(f"추출 조건·분포: {manifest_output}")


if __name__ == "__main__":
    main()
