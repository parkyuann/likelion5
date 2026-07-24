"""KOSIS API 검색 근거를 검토한 모델 라벨을 별도 gold 파일로 기록한다.

원본 annotation sheet는 보존한다. 이 결과는 후보 순위만으로 정답을 정하지 않고,
공식 KOSIS 검색 결과와 주장 범위를 함께 검토한 초기 adjudication이다.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# 지표·대상·기간이 공식 검색 결과의 표명과 직접 맞는 경우만 표를 확정한다.
MATCHES = {
    "retrieval_gold_v1_0003": ("101:DT_1KC2020", "산업별 서비스업생산지수(2020=100.0)"),
    "retrieval_gold_v1_0016": ("301:DT_402Y016", "수출물가지수(품목별)"),
    "retrieval_gold_v1_0017": ("388:TX_38804_A003", "연도별 전력거래량 및 전력거래금액"),
    "retrieval_gold_v1_0020": ("318:TX_31802_A000", "주유소 제품별 평균 판매가격"),
    "retrieval_gold_v1_0022": ("101:DT_1J22004", "신선식품지수(2020=100)"),
    "retrieval_gold_v1_0028": ("301:DT_301Y013", "국제수지"),
    "retrieval_gold_v1_0043": ("127:DT_127012_01410", "신규채용"),
    "retrieval_gold_v1_0048": ("142:DT_34006H_034__1", "주요국 대비 기술격차"),
    "retrieval_gold_v1_0053": ("101:DT_1F70011", "설비투자지수"),
    "retrieval_gold_v1_0055": ("101:DT_1L6E001", "소득분배지표"),
    "retrieval_gold_v1_0057": ("101:DT_2KAAD05", "국가경쟁력 순위"),
    "retrieval_gold_v1_0061": ("101:DT_1B81A17", "시군구/합계출산율, 모의 연령별 출산율"),
    "retrieval_gold_v1_0064": ("101:DT_104Y282", "시도별 주택시가총액(명목, 연말기준)"),
    "retrieval_gold_v1_0067": ("101:DT_1KC2020", "산업별 서비스업생산지수(2020=100.0)"),
    "retrieval_gold_v1_0068": ("101:DT_2KAA2301", "소매판매액지수"),
    "retrieval_gold_v1_0069": ("301:DT_511Y002", "소비자동향조사(전국, 월, 2008.9~)"),
    "retrieval_gold_v1_0076": ("101:DT_1B8000H", "시도/인구동태건수 및 동태율(출생,사망,혼인,이혼)"),
    "retrieval_gold_v1_0078": ("127:DT_CFA0001_S10502", "주요 국가별 기술별 기술수출 추이"),
    "retrieval_gold_v1_0102": ("152:DT_15205_100", "대규모기업집단 지정현황"),
    "retrieval_gold_v1_0104": ("301:DT_200Y102", "주요지표(분기지표)"),
    "retrieval_gold_v1_0111": ("101:DT_1DE7110", "연령/산업/직업별 비정규직 근로자 규모 및 비중"),
    "retrieval_gold_v1_0113": ("101:DT_1DE7110", "연령/산업/직업별 비정규직 근로자 규모 및 비중"),
    "retrieval_gold_v1_0114": ("301:DT_200Y102", "주요지표(분기지표)"),
}

# 기업 개별 실적, 해외만의 지표, 정책·일회성 집계처럼 KOSIS 표 범위가 아닌 경우다.
NO_KOSIS_MATCH = {
    "retrieval_gold_v1_0001", "retrieval_gold_v1_0002", "retrieval_gold_v1_0004", "retrieval_gold_v1_0005",
    "retrieval_gold_v1_0009", "retrieval_gold_v1_0010", "retrieval_gold_v1_0011", "retrieval_gold_v1_0012",
    "retrieval_gold_v1_0013", "retrieval_gold_v1_0018", "retrieval_gold_v1_0024", "retrieval_gold_v1_0027",
    "retrieval_gold_v1_0030", "retrieval_gold_v1_0032", "retrieval_gold_v1_0035", "retrieval_gold_v1_0036",
    "retrieval_gold_v1_0038", "retrieval_gold_v1_0039", "retrieval_gold_v1_0044", "retrieval_gold_v1_0046",
    "retrieval_gold_v1_0050", "retrieval_gold_v1_0059", "retrieval_gold_v1_0063", "retrieval_gold_v1_0065",
    "retrieval_gold_v1_0066", "retrieval_gold_v1_0073", "retrieval_gold_v1_0077", "retrieval_gold_v1_0080",
    "retrieval_gold_v1_0081", "retrieval_gold_v1_0083", "retrieval_gold_v1_0085", "retrieval_gold_v1_0094",
    "retrieval_gold_v1_0095", "retrieval_gold_v1_0098", "retrieval_gold_v1_0101", "retrieval_gold_v1_0107",
    "retrieval_gold_v1_0110", "retrieval_gold_v1_0116", "retrieval_gold_v1_0117",
}
SKIP = {"retrieval_gold_v1_0062"}


def official_url(table_key: str) -> str:
    """표 키로 사람이 다시 확인할 수 있는 KOSIS 공식 표 URL을 만든다."""
    org_id, tbl_id = table_key.split(":", maxsplit=1)
    return f"https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"


def apply_labels(input_path: Path, output_path: Path) -> dict[str, int]:
    """보수적으로 MATCH만 표 키를 채우고 나머지는 no-match 또는 보류로 남긴다."""
    with input_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0]) if rows else []
    for row in rows:
        eval_id = row["gold_eval_id"]
        row["review_status"] = "adjudicated"
        row["reviewer"] = "codex_api_evidence_20260724"
        row["reviewed_at"] = date.today().isoformat()
        if eval_id in MATCHES:
            table_key, table_name = MATCHES[eval_id]
            org_id, tbl_id = table_key.split(":", maxsplit=1)
            row.update({
                "gold_match_status": "MATCH",
                "gold_table_key": table_key,
                "gold_org_id": org_id,
                "gold_tbl_id": tbl_id,
                "gold_tbl_name": table_name,
                "official_evidence_url": official_url(table_key),
                "review_notes": "KOSIS 검색 API 표명과 주장 지표·대상 범위가 직접 일치하는 것으로 확인.",
            })
        elif eval_id in NO_KOSIS_MATCH:
            row.update({
                "gold_match_status": "NO_KOSIS_MATCH",
                "review_notes": "기업 개별·해외 전용·정책/일회성 수치로 판단되어 KOSIS 집계통계 표를 확정하지 않음.",
            })
        elif eval_id in SKIP:
            row.update({
                "gold_match_status": "SKIP",
                "review_notes": "전망·예측 표현이므로 통계표 매핑 평가 대상에서 제외.",
            })
        else:
            row.update({
                "gold_match_status": "AMBIGUOUS",
                "review_notes": "KOSIS 검색 결과만으로 주장에 필요한 대상·시점·조건과 단일 표의 일치를 확정할 수 없어 보류.",
            })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return {status: sum(row["gold_match_status"] == status for row in rows) for status in ("MATCH", "NO_KOSIS_MATCH", "AMBIGUOUS", "SKIP")}


if __name__ == "__main__":
    result = apply_labels(
        ROOT / "data/retrieval_gold_v1_annotation_20260724.csv",
        ROOT / "data/retrieval_gold_v1_model_adjudicated_20260724.csv",
    )
    print(result)
