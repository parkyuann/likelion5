"""
Task 3 (claim_class 라벨링 파일럿) 1단계: 층화 표본 추출 + 라벨링용 엑셀 생성.

data/claim_candidates_full.csv(7,043건)에서 검색_구분_레이블(True/False) x
change_type 조합별로 표본을 뽑아 사람이 claim_class/source_scope를 채워 넣을
엑셀 파일을 만든다. claim_class/source_scope 값 목록은
docs/claim_extraction_schema.md 2-2절을 그대로 따른다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/claim_pilot_sample.py --n 40
"""
import argparse
import sys
from pathlib import Path

import pandas as pd
from openpyxl.worksheet.datavalidation import DataValidation

CLAIM_CLASS_VALUES = [
    "집계통계", "개별사례", "전망예측", "목표계획", "법령제도",
    "해석수사", "사고대응임시통계", "여론조사", "통계조사안내", "정정보도",
]
SOURCE_SCOPE_VALUES = ["KOSIS계열", "타공식기관", "민간기관", "해외기관", "불명"]

INPUT_PATH = Path("data/claim_candidates_full.csv")
OUTPUT_PATH = Path("data/claim_class_labeling_pilot.xlsx")

LABEL_COLUMNS = [
    "claim_class", "source_scope", "verifiability_prefilter_사람판단",
    "라벨러", "메모",
]
CONTEXT_COLUMNS = [
    "article_idx", "기사제목", "작성일", "검색_구분_레이블",
    "claim_text", "value_list", "unit_list", "change_type",
    "time_ref", "time_compare", "source_mentioned", "source_org_raw",
]


def stratified_sample(df: pd.DataFrame, n_total: int, seed: int) -> pd.DataFrame:
    groups = df.groupby(["검색_구분_레이블", "change_type"])
    n_cells = groups.ngroups
    per_cell = max(1, n_total // n_cells)

    parts = [g.sample(min(len(g), per_cell), random_state=seed) for _, g in groups]
    picked = pd.concat(parts, ignore_index=True)

    # 목표치에 못 미치면(작은 셀이 많아서) 남은 행에서 무작위로 채운다
    if len(picked) < n_total:
        remaining = df[~df["claim_text"].isin(picked["claim_text"])]
        fill = remaining.sample(min(n_total - len(picked), len(remaining)), random_state=seed)
        picked = pd.concat([picked, fill], ignore_index=True)

    return picked.sample(frac=1, random_state=seed).reset_index(drop=True)


def write_labeling_workbook(sample: pd.DataFrame, out_path: Path) -> None:
    out = sample[CONTEXT_COLUMNS].copy()
    for col in LABEL_COLUMNS:
        out[col] = ""

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="라벨링", index=False)

        codebook = pd.DataFrame({
            "claim_class (9종+1)": CLAIM_CLASS_VALUES + [""] * (len(SOURCE_SCOPE_VALUES) - 0),
        })
        # 두 값 목록 길이를 맞춰 한 시트에 나란히 배치
        max_len = max(len(CLAIM_CLASS_VALUES), len(SOURCE_SCOPE_VALUES))
        codebook = pd.DataFrame({
            "claim_class": CLAIM_CLASS_VALUES + [""] * (max_len - len(CLAIM_CLASS_VALUES)),
            "source_scope": SOURCE_SCOPE_VALUES + [""] * (max_len - len(SOURCE_SCOPE_VALUES)),
        })
        codebook.to_excel(writer, sheet_name="코드북", index=False)

        note = pd.DataFrame({
            "안내": [
                "claim_class, source_scope는 반드시 코드북 시트의 값 중 하나로 입력 (드롭다운 제공).",
                "verifiability_prefilter_사람판단: claim_class=집계통계 AND source_scope=KOSIS계열 이면 '검증시도', 아니면 '판단불가(범위밖)'.",
                "판단 기준 상세는 docs/claim_extraction_schema.md 2-2절, 3장(실제 사례) 참고.",
                "라벨러 칸에 본인 이름만 적어주세요 (누가 라벨링했는지 추적용).",
            ]
        })
        note.to_excel(writer, sheet_name="안내", index=False)

    from openpyxl import load_workbook
    wb = load_workbook(out_path)
    ws = wb["라벨링"]

    col_letters = {name: chr(ord("A") + i) for i, name in enumerate(out.columns)}
    n_rows = len(out) + 1

    dv_class = DataValidation(type="list", formula1="코드북!$A$2:$A$11", allow_blank=True)
    dv_scope = DataValidation(type="list", formula1="코드북!$B$2:$B$6", allow_blank=True)
    ws.add_data_validation(dv_class)
    ws.add_data_validation(dv_scope)
    dv_class.add(f"{col_letters['claim_class']}2:{col_letters['claim_class']}{n_rows}")
    dv_scope.add(f"{col_letters['source_scope']}2:{col_letters['source_scope']}{n_rows}")

    wb.save(out_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=40, help="목표 표본 크기 (30~50 권장)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    df = pd.read_csv(INPUT_PATH)
    sample = stratified_sample(df, args.n, args.seed)
    write_labeling_workbook(sample, OUTPUT_PATH)

    print(f"표본 {len(sample)}건 -> {OUTPUT_PATH}")
    print(sample.groupby(["검색_구분_레이블", "change_type"]).size())


if __name__ == "__main__":
    main()
