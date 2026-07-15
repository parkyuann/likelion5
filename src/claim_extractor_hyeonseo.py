"""
claim_extractor.py + claim_value_time_matcher.py(나열형 문장 값-시점 대응 후처리) 통합 파이프라인.

claim_extractor.py(문장에서 값-단위 후보 행 추출)와 claim_value_time_matcher.py(나열형 문장을
값-시점/값-개체 단위로 재매칭)를 이어붙여, 중간 산출물 claim_candidates_relaxed.csv를 디스크에
쓰지 않고 news_preprocessed.csv에서 곧장 claim_candidates_relaxed_2.csv를 만든다.

두 원본 파일은 이 파일에서 절대 수정하지 않고 함수만 그대로 가져다 쓴다
(extract_from_article은 claim_extractor.py, postprocess_rows는 claim_value_time_matcher.py).

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/claim_extractor_hyeonseo.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claim_extractor import extract_from_article  # noqa: E402
from claim_value_time_matcher import postprocess_rows  # noqa: E402

INPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "news_preprocessed.csv"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "claim_candidates_relaxed_2.csv"


def main():
    df = pd.read_csv(INPUT_PATH)
    all_rows = []
    for idx, row in df.iterrows():
        text = row.get("본문_정제")
        if not isinstance(text, str) or not text:
            continue
        all_rows.extend(
            extract_from_article(idx, row["기사제목"], row["작성일"], row["검색 구분 레이블"], text)
        )

    expanded_rows = postprocess_rows(all_rows)

    result = pd.DataFrame(expanded_rows)
    result.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

    n_articles_with_claims = result["article_idx"].nunique() if len(result) else 0
    print(f"기사 {len(df)}건 중 {n_articles_with_claims}건에서 후보 문장 {len(all_rows)}개 추출 "
          f"-> 나열형 재매칭 후 {len(result)}행(증가분 {len(result) - len(all_rows)}행)")
    print(f"레이블별 후보 문장 수:\n{result.groupby('검색_구분_레이블').size()}")
    print(f"change_type 분포:\n{result['change_type'].value_counts()}")
    print(f"candidate_origin 분포:\n{result['candidate_origin'].value_counts()}")
    print(f"저장 위치: {OUTPUT_PATH}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
