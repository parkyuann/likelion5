# -*- coding: utf-8 -*-
"""candidate_labeling_pilot_relaxed.csv 의 gold_* 컬럼을 채우는 스크립트.

행 1개 = 후보 주장 1개. 각 후보가 실제 '집계통계 주장'인지 판정(gold_is_aggregate_claim)하고,
맞는 경우에만 지표/값/출처 등 세부 필드를 기록한다.
"""
import csv
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "labeling" / "candidate_labeling_pilot_relaxed.csv"
ANNOTATOR = "claude"

# 채울 컬럼 목록
GOLD_COLS = [
    "gold_is_aggregate_claim", "gold_claim_class", "gold_source_scope",
    "gold_verifiability_prefilter", "gold_indicator_raw", "gold_population",
    "gold_value", "gold_unit", "gold_time_ref", "gold_time_compare",
    "gold_source_org_raw", "gold_source_role", "gold_source_evidence_quote",
    "gold_notes", "annotator",
]

def NO(reason):
    """집계통계 주장이 아닌 후보."""
    return {"gold_is_aggregate_claim": "False", "gold_notes": reason, "annotator": ANNOTATOR}

def YES(**kw):
    kw.setdefault("gold_is_aggregate_claim", "True")
    kw.setdefault("gold_claim_class", "집계통계")
    kw["annotator"] = ANNOTATOR
    return kw

CAND = {}

# ---- 집계통계 주장으로 인정 (True) ----
CAND["C009"] = YES(
    gold_source_scope="해외기관", gold_verifiability_prefilter="False",
    gold_indicator_raw="글로벌 주류(맥주) 소비량", gold_population="전 세계",
    gold_value="4", gold_unit="%", gold_time_ref="현재", gold_time_compare="2018년",
    gold_source_org_raw="", gold_source_role="미상",
    gold_source_evidence_quote="2018년(4968억병)보다 약 4% 감소한 수치다.",
    gold_notes="글로벌 소비량 관련 집계통계이나 출처·지표 정의 불명확(2018년 4968억병 대비 약 4% 감소).")

CAND["C010"] = YES(
    gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="가공식품의 생활물가 상승률 기여도", gold_population="전국",
    gold_value="0.34", gold_unit="%p", gold_time_ref="올해", gold_time_compare="작년 하반기(0.15%p)",
    gold_source_org_raw="한국은행", gold_source_role="발표기관",
    gold_source_evidence_quote="가공식품의 생활 물가 상승률 기여도는 작년 하반기 0.15%포인트에서 올해 0.34%포인트로 두 배 이상 확대됐다.",
    gold_notes="작년 하반기 0.15%p→올해 0.34%p 증감폭.")

CAND["C011"] = YES(
    gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="품목별 소비자물가 등락률", gold_population="전국",
    gold_value="배추 -34.5; 무 -40.5; 쌀 21.3; 사과 21.6; 달걀 6.9", gold_unit="%",
    gold_time_ref="2025년 10월", gold_time_compare="전년 동월",
    gold_source_org_raw="통계청(국가데이터처)", gold_source_role="발표기관",
    gold_source_evidence_quote="배추(-34.5%), 무(-40.5%) 등은 마이너스 물가 상승률을 보였지만, 쌀(21.3%), 사과(21.6%), 달걀(6.9%) 등은",
    gold_notes="10월 소비자물가 품목별 등락률.")

CAND["C014"] = YES(
    gold_source_scope="민간기관", gold_verifiability_prefilter="False",
    gold_indicator_raw="3040세대 모임통장 평균 잔액", gold_population="3040세대",
    gold_value="197만", gold_unit="원", gold_time_ref="", gold_time_compare="전체 연령대 평균(153만원)",
    gold_source_org_raw="", gold_source_role="미상(은행 데이터 추정)",
    gold_source_evidence_quote="모임 통장의 평균 잔액은 197만원으로 같은 연령대 모임 통장 전체 평균 잔액(153만원)보다 약 30% 많았다.",
    gold_notes="민간 은행 상품 데이터 기반 집계로 KOSIS 검증 불가.")

CAND["C017"] = YES(
    gold_source_scope="국내공공기관", gold_verifiability_prefilter="True",
    gold_indicator_raw="대구 휘발유 평균 판매가격", gold_population="대구",
    gold_value="1699.5", gold_unit="원", gold_time_ref="2025년 2월", gold_time_compare="전주",
    gold_source_org_raw="한국석유공사 오피넷(기사 출처)", gold_source_role="발표기관",
    gold_source_evidence_quote="대구는 전주 대비 3.4원 하락한 1699.5원을 기록",
    gold_notes="전주比 3.4원 하락. 후보 행에는 출처패턴 없으나 기사 출처는 오피넷.")

CAND["C022"] = YES(
    gold_source_scope="국내정부기관", gold_verifiability_prefilter="True",
    gold_indicator_raw="러브버그 관련 민원 건수", gold_population="서울시",
    gold_value="9296", gold_unit="건", gold_time_ref="지난해", gold_time_compare="1년 전(4418건)",
    gold_source_org_raw="서울시", gold_source_role="발표기관",
    gold_source_evidence_quote="지난해 서울시에 접수된 러브버그 관련 민원은 9296건으로 1년 전 4418건 대비 두 배 가까이 증가했다.",
    gold_notes="지자체 행정 민원 집계통계.")

CAND["C026"] = YES(
    gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="인구 증가 수(전월대비)", gold_population="특정 시(전국 1위)",
    gold_value="4205", gold_unit="명", gold_time_ref="해당 월", gold_time_compare="전월",
    gold_source_org_raw="", gold_source_role="미상(주민등록 인구통계 추정)",
    gold_source_evidence_quote="전월 대비 4205명 늘어난 숫자로, 이는 전국 17개 시·도 중 가장 큰 증가 폭이다.",
    gold_notes="주민등록 인구 증가 집계, 대상 지역 특정 필요.")

CAND["C031"] = YES(
    gold_source_scope="민간기관", gold_verifiability_prefilter="False",
    gold_indicator_raw="저축성 자산의 금융자산 내 비중", gold_population="(보고서 대상)",
    gold_value="45", gold_unit="%", gold_time_ref="2023년", gold_time_compare="2022년(42%)",
    gold_source_org_raw="보고서", gold_source_role="인용",
    gold_source_evidence_quote="2022년 저축성 자산은 금융자산의 42%를 차지했고 2023년 금리 상승과 함께 45%까지 상승했다.",
    gold_notes="민간 보고서 기반, 모집단 정의 불명확.")

CAND["C032"] = YES(
    gold_source_scope="국내공공기관", gold_verifiability_prefilter="True",
    gold_indicator_raw="전국 주유소 휘발유 평균 판매가격", gold_population="전국",
    gold_value="1726.9", gold_unit="원", gold_time_ref="2025년 2월 셋째 주", gold_time_compare="전주",
    gold_source_org_raw="한국석유공사 오피넷", gold_source_role="발표기관",
    gold_source_evidence_quote="전국 주유소 휘발유 평균 판매가격은 전주 대비 리터(L)당 4원 하락한 1726.9원을 기록했다.",
    gold_notes="전주比 4원 하락.")

CAND["C038"] = YES(
    gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="근로자 가구 월평균 근로소득", gold_population="근로자 가구",
    gold_value="506만", gold_unit="원", gold_time_ref="지난해 3분기", gold_time_compare="전년 동기(493만원)",
    gold_source_org_raw="통계청(가계동향조사 추정)", gold_source_role="발표기관",
    gold_source_evidence_quote="지난해 3분기 근로자 가구의 월평균 근로소득은 506만원으로 전년 같은 기간(493만원)보다 2.6% 늘었다.",
    gold_notes="전년동기比 2.6% 증가.")

CAND["C040"] = YES(
    gold_source_scope="국내공공기관", gold_verifiability_prefilter="True",
    gold_indicator_raw="내국인 관광객 수", gold_population="(전남 지역)",
    gold_value="3만7105", gold_unit="명", gold_time_ref="해당 기간", gold_time_compare="전년(-16.7%)",
    gold_source_org_raw="", gold_source_role="미상(관광통계 추정)",
    gold_source_evidence_quote="내국인 관광객은 전년 대비 3만7105명(16.7%) 줄었다.",
    gold_notes="관광객 수 집계, 전년比 16.7% 감소.")

CAND["C042"] = YES(
    gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="투입 물가 누적 상승률(가공식품/외식/개인서비스)", gold_population="전국",
    gold_value="가공식품 30.4; 외식 24.1; 외식외 개인서비스 17.4", gold_unit="%",
    gold_time_ref="올해 4월", gold_time_compare="",
    gold_source_org_raw="한국은행", gold_source_role="발표기관",
    gold_source_evidence_quote="올해 4월 기준 투입 물가의 누적 상승률이 가공식품 30.4%, 외식 24.1%, 외식 외 개인 서비스 17.4% 등이었다.",
    gold_notes="한은 보고서 누적 상승률.")

CAND["C045"] = YES(
    gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="국내 주식시장 시가총액", gold_population="국내 증시",
    gold_value="2324조", gold_unit="원", gold_time_ref="2025년 3월 말", gold_time_compare="1월 초(2254조원)",
    gold_source_org_raw="한국거래소(추정)", gold_source_role="미상",
    gold_source_evidence_quote="올해 3월 말 기준 국내 주식시장 시총은 2324조원으로 1월 초(2254조원)보다 70조원 늘었다.",
    gold_notes="시가총액 집계, 1월 초比 70조원 증가.")

# ---- 집계통계 주장 아님 (False) ----
CAND["C001"] = NO("세계 1위 순위 중심이며 매출 주체(모집단)가 모호.")
CAND["C002"] = NO("동영상 플레이어 UI 텍스트(노이즈).")
CAND["C003"] = NO("개별 차종(GV60) 주행거리 제원이라 집계통계 아님.")
CAND["C004"] = NO("최저임금 정책 결정에서 파생된 환산 월급 금액.")
CAND["C005"] = NO("개별 기업(파라다이스시티) 매출 수치.")
CAND["C006"] = NO("북한 선원 8년 등 개인 사례·증언.")
CAND["C007"] = NO("의협 회장 선거 득표 순위.")
CAND["C008"] = NO("주식 부자 순위.")
CAND["C012"] = NO("인물 서품·재임 연도 등 단순 날짜/기간.")
CAND["C013"] = NO("세대 구분용 근속연수 언급, 통계 아님.")
CAND["C015"] = NO("일본 마이너스 금리 종료라는 정책 사건·기간.")
CAND["C016"] = NO("공공부채 비율 증가 전망(예측).")
CAND["C018"] = NO("BCG 선정 혁신기업 순위.")
CAND["C019"] = NO("암호화폐 등락률(특정 시점 시장 시세).")
CAND["C020"] = NO("사건 경과 기간(단순 수치).")
CAND["C021"] = NO("원전 착공 기수 역사적 서술(단순 수치).")
CAND["C023"] = NO("예산 감액의 성장률 영향 분석(전망).")
CAND["C024"] = NO("비트코인 시세(특정 시점 시장 시세).")
CAND["C025"] = NO("삼성전자 세계 반도체 1위(개별 기업 순위).")
CAND["C027"] = NO("하이브리드 차량 연비·출력 제원.")
CAND["C028"] = NO("영국·멕시코 기준금리 인하폭(정책 결정).")
CAND["C029"] = NO("대미·대멕시코 수출 급감 전망(예측).")
CAND["C030"] = NO("원화 환율 시세(특정 시점 시장 시세).")
CAND["C033"] = NO("일본은행 물가 목표치 2%(정책 목표).")
CAND["C034"] = NO("개별 기업(투썸플레이스) 가격 인상.")
CAND["C035"] = NO("미 국채 응찰률(개별 입찰 시장 지표).")
CAND["C036"] = NO("개별 은행(신한) 가산금리 인하 조치.")
CAND["C037"] = NO("열차 취소 위약금률(제도 기준).")
CAND["C039"] = NO("소득공제율 상향 공약(정책 계획).")
CAND["C041"] = NO("'20년이 걸리더라도' 등 수사적 표현.")
CAND["C043"] = NO("원화 환율 시세(특정 시점 시장 시세).")
CAND["C044"] = NO("금리 인하 이자 절감 효과 예시 계산.")
CAND["C046"] = NO("IMF 전망 지속 기간(예측/단순 수치).")
CAND["C047"] = NO("개별 금융사고 발생 기간·피해 금액.")
CAND["C048"] = NO("개별 은행 가산금리 인하 시점(개월 수).")
CAND["C049"] = NO("개별 기업 지분율·거래 금액.")
CAND["C050"] = NO("자막 설정 UI 텍스트(노이즈).")

# ================================================================ write
with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    rows = list(reader)

missing = [c for c in GOLD_COLS if c not in fieldnames]
assert not missing, f"CSV에 없는 컬럼: {missing}"

filled = 0
for row in rows:
    sid = row["sample_id"]
    if sid not in CAND:
        continue
    vals = CAND[sid]
    for col in GOLD_COLS:
        row[col] = vals.get(col, "")
    filled += 1

with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

n_true = sum(1 for s in CAND if CAND[s].get("gold_is_aggregate_claim") == "True")
print(f"filled rows: {filled} / {len(rows)}")
print(f"aggregate=True : {n_true}")
print(f"aggregate=False: {len(CAND) - n_true}")
