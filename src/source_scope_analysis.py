"""
source_scope 재계산 — "KOSIS계열=통계청 하나"로 좁게 정의하면 뉴스 출처의 몇 %가
잘못 배제되는가를, 전체 크롤(30/30, 380개 org / 181개 정식 기관명) 기준으로 다시 계산.

이전 계산(mentoring_questions.md의 55.1%)은 대분류 13/30 크롤 시점의 85개 기관
목록으로 한 하한선이었다. 이제 전체 크롤이 끝나 기관 목록이 완전해졌으므로 갱신한다.

방법:
  - 뉴스 후보 문장(claim_candidates_full.csv) 중 출처가 명시된 것(source_org_raw)의
    고유 출처 문자열을 KOSIS 정식 기관 목록(data/kosis_org_names.json, 181개)과 대조.
  - A: 통계청/국가데이터처(org 101) — 좁은 정의로도 포함
  - B: KOSIS 등재기관인데 통계청이 아님 — 좁은 정의로는 "잘못" 배제됨
  - C: KOSIS 밖(민간·해외 등) — 정상 배제
  - D: 기관명이 아님(추출 노이즈: 보고서/자료/통계/결과 등)
  - 공식기관 인용(A+B) 중 B 비율 = "통계청이 아니라는 이유만으로 배제되는 비율"

실행: venv/Scripts/python.exe src/source_scope_analysis.py
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "data" / "claim_candidates_full.csv"
ORG_NAMES = ROOT / "data" / "kosis_org_names.json"

# 통계청 = 국가데이터처(2024 개편으로 개명). org_id 101.
STAT_OFFICE_NAMES = {"통계청", "국가데이터처", "국가데이터"}

# 뉴스에서 흔히 쓰는 약칭 → KOSIS 정식 기관명. (정식명이 그대로 나오면 매칭되므로
# 약칭만 보완한다.)
ALIASES = {
    "한은": "한국은행",
    "공정위": "공정거래위원회",
    "기재부": "기획재정부",
    "국토부": "국토교통부",
    "산업부": "산업통상자원부",
    "복지부": "보건복지부",
    "고용부": "고용노동부",
    "노동부": "고용노동부",
    "행안부": "행정안전부",
    "금융위": "금융위원회",
    "금감원": "금융감독원",
    "관세청": "관세청",
    "농식품부": "농림축산식품부",
    "해수부": "해양수산부",
    "중기부": "중소벤처기업부",
    "교육부": "교육부",
    "환경부": "환경부",
    # KOSIS 기관 목록이 옛 부처명을 쓰는 경우(실측 확인)
    "산업통상자원부": "산업통상부",
    "산업부": "산업통상부",
}

# 기관명이 아닌 게 명백한 추출 노이즈(정규식이 "OOO에 따르면"에서 잘못 집은 것들).
NOISE_TOKENS = {
    "보고서", "자료", "통계", "결과", "업계", "금융권", "조사", "분석", "발표",
    "정부", "당국", "관계자", "설명", "예상", "전망", "이번", "관련", "기준",
    "내용", "수치", "집계", "추정", "계획", "방침", "예정", "현재", "지난해",
    "올해", "최근", "이후", "가운데", "위원회", "협의회", "보도", "소식",
}


def load_org_names() -> dict:
    return json.loads(ORG_NAMES.read_text(encoding="utf-8"))


def classify_source(raw: str, org_names_set: set) -> str:
    """A/B/C/D 중 하나로 분류."""
    s = str(raw).strip()
    # A: 통계청/국가데이터처
    if any(name in s for name in STAT_OFFICE_NAMES):
        return "A"
    # 약칭 정규화
    canonical = ALIASES.get(s, s)
    # B: KOSIS 정식 기관명과 매칭(양방향 부분일치)
    for org in org_names_set:
        if org in canonical or canonical in org:
            # 너무 짧은 우연 매칭 방지(2자 이상 겹칠 때만)
            if len(canonical) >= 2 and (org in canonical or len(canonical) >= 3):
                return "B"
    # D: 노이즈 토큰
    if s in NOISE_TOKENS or len(s) < 2:
        return "D"
    # C: 그 외 실제 기관으로 보이나 KOSIS 밖(민간/해외/협회 등)
    return "C"


def main():
    df = pd.read_csv(CANDIDATES)
    srcs = df[df["source_mentioned"] == True]["source_org_raw"].dropna()
    total = len(srcs)

    org_names = load_org_names()
    # 통계청(101)은 A로 따로 처리하므로 B 매칭 대상에서 제외
    org_names_set = {v for k, v in org_names.items() if k != "101"}

    labels = srcs.map(lambda s: classify_source(s, org_names_set))
    counts = labels.value_counts().to_dict()
    a = counts.get("A", 0)
    b = counts.get("B", 0)
    c = counts.get("C", 0)
    d = counts.get("D", 0)

    print(f"출처 명시 문장: {total}건 (고유 {srcs.nunique()}종)")
    print(f"KOSIS 정식 기관 목록: {len(org_names)}개 (전체 크롤 org 380개 중 이름 확인된 것)")
    print()
    print(f"| 분류 | 건수 | 비율 |")
    print(f"|---|---|---|")
    print(f"| A. 통계청/국가데이터처(좁은 정의로도 포함) | {a} | {a/total*100:.1f}% |")
    print(f"| B. KOSIS 등재기관인데 좁은 정의로 잘못 배제 | {b} | {b/total*100:.1f}% |")
    print(f"| C. KOSIS 밖(민간·해외) | {c} | {c/total*100:.1f}% |")
    print(f"| D. 추출 노이즈 | {d} | {d/total*100:.1f}% |")
    print()
    if a + b > 0:
        print(f"공식기관 인용(A+B={a+b}건) 중 B 비율 = {b/(a+b)*100:.1f}% "
              f"(\"통계청이 아니라는 이유만으로\" 배제)")

    # B/C로 분류된 고유 출처를 검토용으로 출력(수동 확인해 노이즈 오분류 잡기)
    print("\n--- B로 분류된 고유 출처(KOSIS 등재로 판정) ---")
    b_srcs = srcs[labels.values == "B"].value_counts()
    for s, cnt in b_srcs.items():
        print(f"  {cnt:3d}  {s}")
    print("\n--- C로 분류된 상위 고유 출처(KOSIS 밖으로 판정) ---")
    c_srcs = srcs[labels.values == "C"].value_counts()
    for s, cnt in c_srcs.head(25).items():
        print(f"  {cnt:3d}  {s}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
