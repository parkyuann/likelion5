# -*- coding: utf-8 -*-
"""team2 라벨링: 기사 CSV(gold 5컬럼) + 후보 CSV(gold 15컬럼)를 함께 채운다.

판정 기준은 team1과 동일:
집계통계(모집단 단위 통계)만 인정하고, 개인·순위·시장 시세·전망·정책 목표·
제도 기준·개별 기업/기금 실적·UI 노이즈는 제외한다.
"""
import csv
import json
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "data" / "labeling"
ART = DIR / "article_labeling_pilot_relaxed_team2.csv"
CAND = DIR / "candidate_labeling_pilot_relaxed_team2.csv"
ANNOTATOR = "claude"


def C(**kw):
    return {k: v for k, v in kw.items() if v not in ("", None)}


# =====================================================================
# 1) 기사 CSV
# =====================================================================
A = {}

A["A006"] = ([
    C(claim_text="펜타닐로 일할 수 없게 된 미국 노동자는 2022년 말 기준 630만명에 달한다.",
      evidence_quote="펜타닐로 인해 일을 할 수 없게 된 미국 노동자는 2022년 말 기준으로 630만명에 달한다.",
      claim_class="집계통계", indicator_raw="펜타닐로 노동 불능 상태가 된 노동자 수", population="미국",
      value="630만", unit="명", time_ref="2022년 말",
      source_org_raw="미국 질병통제예방센터(CDC)", source_scope="해외기관"),
], "이 중 60% 이상이 25~54세. 제목의 '사망자 연 7만명'은 본문에 구체 수치 없음.")

A["A013"] = ([
    C(claim_text="2024년 영아돌연사증후군으로 숨진 영아는 47명으로 전체 영아 사망의 8.3%를 차지했다.",
      evidence_quote="지난해 영아돌연사증후군으로 숨진 영아는 47명이었다. 전체 영아 사망(출생 후 1년 이내 사망) 중 8.3%를 차지하며, 전년보다 두 명 증가했다.",
      claim_class="집계통계", indicator_raw="영아돌연사증후군 사망자 수", population="전국 영아",
      value="47", unit="명", change_value="2", change_unit="명", change_type="증감폭",
      time_ref="2024년", time_compare="전년",
      source_org_raw="국가데이터처(2024년 사망원인통계)", source_scope="KOSIS계열"),
    C(claim_text="2024년 영아돌연사증후군 사망률은 인구 10만 명당 20.4명이었다.",
      evidence_quote="인구 10만 명당 사망률은 20.4명이었다.",
      claim_class="집계통계", indicator_raw="영아돌연사증후군 사망률", population="전국",
      value="20.4", unit="명(인구 10만명당)", time_ref="2024년",
      source_org_raw="국가데이터처", source_scope="KOSIS계열"),
], "응급실 내원 영아 12명(경북대 연구팀)은 개별 연구 표본이라 제외.")

A["A014"] = ([
    C(claim_text="2024년 500대 기업 여성 평균 연봉은 7405만원으로 남성(1억561만원)의 70.1% 수준이었다.",
      evidence_quote="2024년 기준 여성 평균 연봉은 7405만원으로 남성(1억561만원)의 70.1% 수준이었다.",
      claim_class="집계통계", indicator_raw="대기업 성별 평균 연봉", population="국내 500대 기업 직원",
      value="7405만", unit="원", change_value="70.1", change_unit="%", change_type="비중",
      time_ref="2024년", time_compare="2023년(68.6%)",
      source_org_raw="리더스인덱스", source_scope="민간기관"),
    C(claim_text="여성 평균 근속연수는 9.2년으로 남성(11.9년)의 77.3% 수준이다.",
      evidence_quote="여성의 평균 근속연수는 9.2년으로 남성 11.9년의 77.3%지만",
      claim_class="집계통계", indicator_raw="대기업 성별 평균 근속연수", population="국내 500대 기업 직원",
      value="9.2", unit="년", time_compare="남성 11.9년",
      source_org_raw="리더스인덱스", source_scope="민간기관"),
    C(claim_text="대기업 여성 직원 비율은 전체의 26.4%에 불과하다.",
      evidence_quote="대기업에서 여성 비율이 전체 직원의 26.4%에 불과하고",
      claim_class="집계통계", indicator_raw="대기업 여성 직원 비율", population="국내 500대 기업 직원",
      value="26.4", unit="%",
      source_org_raw="리더스인덱스", source_scope="민간기관"),
], "업종별(상사 60.8% 등)·개별 기업(서연이화 등) 수치는 개별 단위라 제외.")

A["A016"] = ([
    C(claim_text="작년 국민 1인당 연간 쌀 소비량은 55.8kg으로 2004년(82kg) 대비 32% 줄었다.",
      evidence_quote="작년 국민 1인당 연간 쌀 소비량은 55.8kg으로 지난 2004년(82kg) 대비 32% 줄었다.",
      claim_class="집계통계", indicator_raw="1인당 연간 쌀 소비량", population="전국",
      value="55.8", unit="kg", change_value="32", change_unit="%", change_type="증감률(감소)",
      time_ref="작년(2024년)", time_compare="2004년(82kg)",
      source_org_raw="통계청(양곡소비량조사 추정)", source_scope="KOSIS계열"),
    C(claim_text="같은 기간 쌀 생산량은 500만톤에서 358만5000톤으로 28.3% 줄었다.",
      evidence_quote="같은 기간 쌀 생산량은 500만톤에서 358만5000톤으로 28.3% 줄어드는 데 그쳤다.",
      claim_class="집계통계", indicator_raw="쌀 생산량", population="전국",
      value="358만5000", unit="톤", change_value="28.3", change_unit="%", change_type="증감률(감소)",
      time_compare="2004년(500만톤)",
      source_org_raw="통계청(추정)", source_scope="KOSIS계열"),
], "생산단지 지정·직불금 단가·수출 목표 등은 정책 계획이라 제외.")

A["A018"] = ([
    C(claim_text="작년 떡류 수출액은 9140만달러로 전년보다 17.5% 늘어 역대 최대를 기록했다.",
      evidence_quote="지난해 떡류 수출액은 9140만달러(약 1313억원)로 1억달러에 근접했다. 떡류 수출액은 2023년 7780만달러로 역대 최대치를 달성한 데 이어, 작년 17.5% 더 늘어나 기록을 경신했다.",
      claim_class="집계통계", indicator_raw="떡류 수출액", population="전국",
      value="9140만", unit="달러", change_value="17.5", change_unit="%", change_type="증감률",
      time_ref="작년(2024년)", time_compare="2023년(7780만달러)",
      source_org_raw="농림축산식품부·한국농수산식품유통공사(aT)", source_scope="KOSIS계열"),
], "2019년(3430만달러) 대비 5년간 약 3배. 국가별 수출액은 첫 주장에 부수.")

A["A020"] = ([
    C(claim_text="우울증 환자는 2017년 69만1164명에서 2021년 93만3481명으로 35.1% 늘었다.",
      evidence_quote="2017년 69만1164명이던 우울증 환자는 2021년 93만3481명으로 35.1%가량 늘었다.",
      claim_class="집계통계", indicator_raw="우울증 환자 수", population="전국",
      value="93만3481", unit="명", change_value="35.1", change_unit="%", change_type="증감률",
      time_ref="2021년", time_compare="2017년(69만1164명)",
      source_org_raw="건강보험심사평가원", source_scope="국내공공기관"),
    C(claim_text="한국의 우울증 치료율은 11%로 OECD 국가 중 최저 수준이다(미국 66%).",
      evidence_quote="한국의 우울증 치료율은 11%로 OECD 국가 중 최저 수준이다. 미국의 우울증 치료율은 66%다.",
      claim_class="집계통계", indicator_raw="우울증 치료율", population="한국",
      value="11", unit="%", time_ref="2022년", time_compare="미국 66%",
      source_org_raw="대한우울자살예방학회", source_scope="민간기관"),
], "김하늘 양 살해 사건 경위 등은 개별 사건이라 제외.")

A["A022"] = ([
    C(claim_text="70대 이상 인구가 사상 처음으로 20대 인구를 추월했다.",
      evidence_quote="저출산·고령화가 가팔라지면서 70대 이상 인구가 사상 처음 20대 인구를 추월했다.",
      claim_class="집계통계", indicator_raw="70대 이상 인구·20대 인구", population="전국",
      time_ref="", source_org_raw="국가데이터처(추정)", source_scope="KOSIS계열"),
], "본문(요약)에 구체 인구 수치는 없으나 인구구조 집계통계 주장이라 기록.")

A["A024"] = ([
    C(claim_text="지난 8일 수박 1통 소매가격은 2만6091원으로 전년보다 26.6% 올랐다.",
      evidence_quote="지난 8일 수박 1통의 소매 가격은 2만6091원으로 집계됐다. 전년 대비 26.6%, 평년 대비 31.7%가량 올랐다.",
      claim_class="집계통계", indicator_raw="수박 소매가격", population="전국",
      value="2만6091", unit="원", change_value="26.6", change_unit="%", change_type="증감률",
      time_ref="2025년 7월 8일", time_compare="전년",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
    C(claim_text="오이(가시 계통) 10개 소매가는 1만1922원으로 전년보다 27% 올랐다.",
      evidence_quote="8일 오이(가시 계통) 10개의 소매가는 1만1922원으로 전년 대비 27% 올랐고",
      claim_class="집계통계", indicator_raw="오이 소매가격", population="전국",
      value="1만1922", unit="원", change_value="27", change_unit="%", change_type="증감률",
      time_ref="2025년 7월 8일", time_compare="전년",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
    C(claim_text="깻잎 100g 소매가는 2516원으로 전년보다 25.9% 올랐다.",
      evidence_quote="깻잎 100g도 2516원을 기록해 전년보다 25.9%가량 올랐다.",
      claim_class="집계통계", indicator_raw="깻잎 소매가격", population="전국",
      value="2516", unit="원", change_value="25.9", change_unit="%", change_type="증감률",
      time_ref="2025년 7월 8일", time_compare="전년",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
    C(claim_text="적상추 100g 소매가는 1182원으로 전년보다 4.8% 올랐다.",
      evidence_quote="적상추 100g은 전년보다 4.8%가량 오른 1182원이었다.",
      claim_class="집계통계", indicator_raw="적상추 소매가격", population="전국",
      value="1182", unit="원", change_value="4.8", change_unit="%", change_type="증감률",
      time_ref="2025년 7월 8일", time_compare="전년",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
    C(claim_text="국산 염장 고등어 1손 가격은 6838원으로 전년보다 36% 급등했다.",
      evidence_quote="8일 기준 국산 염장 고등어(1손)의 가격은 6838원으로 전년보다 36% 급등했고",
      claim_class="집계통계", indicator_raw="염장 고등어 소매가격", population="전국",
      value="6838", unit="원", change_value="36", change_unit="%", change_type="증감률",
      time_ref="2025년 7월 8일", time_compare="전년",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
    C(claim_text="물오징어(원양 냉동) 1마리 가격은 4718원으로 전년보다 21.9% 올랐다.",
      evidence_quote="물오징어(원양 냉동) 1마리 가격도 4718원으로 전년보다 21.9% 올랐다.",
      claim_class="집계통계", indicator_raw="물오징어 소매가격", population="전국",
      value="4718", unit="원", change_value="21.9", change_unit="%", change_type="증감률",
      time_ref="2025년 7월 8일", time_compare="전년",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
], "한은의 '기온 1도↑→농산물 0.4~0.5%p↑' 추정치와 기상 기록은 분석·기상자료라 제외.")

A["A026"] = ([
    C(claim_text="5월 초 기준 미국 자동차 딜러의 중고차 재고는 43일분으로 2021년 5월 이후 최저다.",
      evidence_quote="5월 초 기준 자동차 딜러가 보유한 중고차 재고는 43일분이라고 밝혔다. 코로나 팬데믹으로 공급망이 교란됐던 2021년 5월 초 이후 가장 낮은 수준이다.",
      claim_class="집계통계", indicator_raw="중고차 재고(일분)", population="미국",
      value="43", unit="일분", time_ref="2025년 5월 초", time_compare="2021년 5월 이후 최저",
      source_org_raw="콕스 오토모티브", source_scope="해외기관"),
    C(claim_text="미국 50개 베스트셀러 모델 기준 중고차 평균 가격은 2만9000달러로 2개월 연속 상승했다.",
      evidence_quote="미국 내 50개 베스트셀러 모델을 기준으로 한 중고차 평균 가격이 최근 2개월 연속 상승해 2만9000달러에 달한다고 밝혔다.",
      claim_class="집계통계", indicator_raw="중고차 평균 가격", population="미국(50개 베스트셀러 모델)",
      value="2만9000", unit="달러", time_ref="2025년 5월", source_org_raw="콕스 오토모티브", source_scope="해외기관"),
], "시티은행의 북미 생산량 8% 감소는 추산치라 제외.")

A["A027"] = ([
    C(claim_text="3월 7일 기준 미국 증시에 상장된 중국 기업은 286개이며 시가총액은 1조1000억달러에 달한다.",
      evidence_quote="지난 3월 7일 기준으로 미국 증시에는 총 286개의 중국 기업이 상장돼 있으며 이들의 시가총액은 1조1000억달러에 달한다.",
      claim_class="집계통계", indicator_raw="미국 증시 상장 중국 기업 수·시가총액", population="미국 증시 상장 중국 기업",
      value="286", unit="개", time_ref="2025년 3월 7일",
      source_org_raw="미중 경제안보검토위원회(USCC)", source_scope="해외기관"),
], "시가총액 1조1000억달러 포함.")

A["A028"] = ([
    C(claim_text="이달(4월 1~11일) 코스피지수 하루 변동률은 평균 1.97%로 2021년 2월(2.03%) 이후 최고다.",
      evidence_quote="이달(1~11일) 코스피지수의 하루 변동률은 평균 1.97%로 집계됐다... 지난 2021년 2월(2.03%) 이후 4년 2개월 만에 가장 높은 수준이다.",
      claim_class="집계통계", indicator_raw="코스피지수 하루 변동률(월평균)", population="코스피",
      value="1.97", unit="%", time_ref="2025년 4월(1~11일)", time_compare="2021년 2월(2.03%)",
      source_org_raw="한국거래소", source_scope="KOSIS계열"),
], "개별 일자 등락률(4/7 -5.57% 등)과 VKOSPI 특정일 값은 시장 시세라 제외.")

A["A029"] = ([
    C(claim_text="지난달 하이브리드차 수출대수는 전년 동월보다 35.5% 증가한 3만5701대를 기록했다.",
      evidence_quote="지난 달 하이브리드차 수출대수가 전년 동월보다 35.5% 증가한 3만5701대를 기록했다고 17일 밝혔다.",
      claim_class="집계통계", indicator_raw="하이브리드차 수출대수", population="전국",
      value="3만5701", unit="대", change_value="35.5", change_unit="%", change_type="증감률",
      time_ref="지난달(1월)", time_compare="전년 동월",
      source_org_raw="산업통상자원부", source_scope="KOSIS계열"),
    C(claim_text="지난달 자동차 전체 수출대수와 수출액은 전년 대비 각각 17.9%, 19.6% 줄었다.",
      evidence_quote="자동차 전체 수출대수와 수출액이 각각 전년대비 17.9%, 19.6% 줄어든",
      claim_class="집계통계", indicator_raw="자동차 수출대수·수출액", population="전국",
      value="-17.9", unit="%", change_type="증감률(감소)", time_ref="지난달(1월)", time_compare="전년",
      source_org_raw="산업통상자원부", source_scope="KOSIS계열"),
    C(claim_text="지난달 자동차 내수 판매는 전년 대비 9% 감소한 10.6만 대를 기록했다.",
      evidence_quote="지난 달 내수 판매는 전년 대비 9% 감소한 10.6만 대를 기록했다.",
      claim_class="집계통계", indicator_raw="자동차 내수 판매대수", population="전국",
      value="10.6만", unit="대", change_value="9", change_unit="%", change_type="증감률(감소)",
      time_ref="지난달(1월)", time_compare="전년",
      source_org_raw="산업통상자원부", source_scope="KOSIS계열"),
    C(claim_text="1월 자동차 생산량은 전년 대비 18.9% 감소한 29.1만 대를 기록했다.",
      evidence_quote="1월 자동차 생산량은 전년 대비 18.9% 감소한 29.1만 대를 기록했다.",
      claim_class="집계통계", indicator_raw="자동차 생산량", population="전국",
      value="29.1만", unit="대", change_value="18.9", change_unit="%", change_type="증감률(감소)",
      time_ref="2025년 1월", time_compare="전년",
      source_org_raw="산업통상자원부", source_scope="KOSIS계열"),
], "판매 상위 5개 모델 등은 개별 차종이라 제외.")

A["A030"] = ([
    C(claim_text="2013~2024년 민간 소비 증가율은 연평균 2%로 2001~2012년보다 1.6%포인트 낮아졌다.",
      evidence_quote="2013~2024년 민간 소비 증가율은 연평균 2%로 2001~2012년에 비해 1.6%포인트 낮아졌다면서",
      claim_class="집계통계", indicator_raw="민간 소비 증가율(연평균)", population="전국",
      value="2", unit="%", change_value="1.6", change_unit="%p", change_type="증감폭",
      time_ref="2013~2024년", time_compare="2001~2012년",
      source_org_raw="한국은행", source_scope="KOSIS계열"),
    C(claim_text="전체 소비 성향은 2010~2012년 76.5%에서 2022~2024년 70%로 6.5%포인트 하락했다.",
      evidence_quote="전체 소비 성향은 2010~2012년 76.5%에서 2022~2024년 70%로 6.5%포인트 하락했다.",
      claim_class="집계통계", indicator_raw="소비 성향", population="전국",
      value="70", unit="%", change_value="6.5", change_unit="%p", change_type="증감폭",
      time_ref="2022~2024년", time_compare="2010~2012년(76.5%)",
      source_org_raw="한국은행", source_scope="KOSIS계열"),
], "인구구조 영향분(연 0.8%p)과 2025~2030년 둔화폭 1%p 확대는 추정·전망이라 제외.")

# 0건 기사
A["A001"] = ([], "여성 일자리 행사 사진 캡션으로 수치 없음.")
A["A002"] = ([], "G마켓 스타배송 확대(400여 브랜드 등) 개별 기업 정보.")
A["A003"] = ([], "기아 EV4 수상 소식으로 집계통계 없음.")
A["A004"] = ([], "스테이블코인 규제 법안 내용과 비트코인 시세라 집계통계 아님.")
A["A005"] = ([], "인구총조사 실시 안내로, 표본 20%는 조사 설계 수치이지 결과 통계가 아님.")
A["A007"] = ([], "근육 운동 건강 조언·개인 신상만 존재.")
A["A008"] = ([], "임직원 746명·야구 경기 기록 등 개별 기업/경기 정보.")
A["A009"] = ([], "로봇업계 반덤핑 제소로 개별 업체 가격 공세(40%)·제소 정보.")
A["A010"] = ([], "연금 보험료율 9%→13%, 원전 4기→3기 등 정책 계획·목표 수치.")
A["A011"] = ([], "은행 강도 제압 사건으로 수치 없음.")
A["A012"] = ([], "본문 없음.")
A["A015"] = ([], "치과 흉기 난동 개별 사건.")
A["A017"] = ([], "비트코인 사상 최고가 1억5720만원은 특정 시점 시장 시세.")
A["A019"] = ([], "재테크 유튜브의 국채 금리 전망·시장 금리 언급으로 집계통계 주장 아님.")
A["A021"] = ([], "신반포7차 조합 총회 참석·정족수 등 개별 조합 수치.")
A["A023"] = ([], "기업은행 부당대출 882억원 등 개별 사건 금액.")
A["A025"] = ([], "ASF 발생·돼지 6000마리 살처분 등 가축 질병 사고성 집계.")


# =====================================================================
# 2) 후보 CSV
# =====================================================================
CAND_COLS = [
    "gold_is_aggregate_claim", "gold_claim_class", "gold_source_scope",
    "gold_verifiability_prefilter", "gold_indicator_raw", "gold_population",
    "gold_value", "gold_unit", "gold_time_ref", "gold_time_compare",
    "gold_source_org_raw", "gold_source_role", "gold_source_evidence_quote",
    "gold_notes", "annotator",
]


def NO(reason):
    return {"gold_is_aggregate_claim": "False", "gold_notes": reason, "annotator": ANNOTATOR}


def YES(**kw):
    kw.setdefault("gold_is_aggregate_claim", "True")
    kw.setdefault("gold_claim_class", "집계통계")
    kw["annotator"] = ANNOTATOR
    return kw


CD = {}

CD["C001"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="기초생활수급자 비율·재정자주도", gold_population="세종시",
    gold_value="0.4; 62.3", gold_unit="%", gold_time_compare="전국 평균(재정자주도 64.9%)",
    gold_source_org_raw="", gold_source_role="미상(행정·재정 통계)",
    gold_source_evidence_quote="세종은 기초생활수급자 비율이 0.4%로 전국에서 가장 낮은 반면, 재정자주도는 62.3%로 전국 평균(64.9%)에서 크게 벗어나지 않는다.",
    gold_notes="지역 행정통계(수급자 비율·재정자주도).")

CD["C004"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="4대 은행 예대금리차", gold_population="4대 은행",
    gold_value="0.23~0.71", gold_unit="%p", gold_time_ref="작년 8월", gold_time_compare="올해 4월",
    gold_source_org_raw="은행연합회 공시(추정)", gold_source_role="미상",
    gold_source_evidence_quote="금리 인하 전인 작년 8월 4대 은행 예대금리 차는 0.23~0.71%포인트였는데, 올해 4월엔 훨씬 벌어졌다.",
    gold_notes="예대금리차 공시 통계.")

CD["C005"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="주택담보대출 연체율", gold_population="전국",
    gold_value="0.34", gold_unit="%", gold_time_ref="1월",
    gold_source_org_raw="", gold_source_role="미상(금융 통계)",
    gold_source_evidence_quote="1월(0.34%)에 이어 두 달 연속 최고치를 경신한 것이다.",
    gold_notes="주담대 연체율 두 달 연속 최고치.")

CD["C006"] = YES(gold_source_scope="해외기관", gold_verifiability_prefilter="False",
    gold_indicator_raw="한계기업 비율 상승폭", gold_population="(국가 비교)",
    gold_value="12.3", gold_unit="%p", gold_time_compare="미국(15.8%p)",
    gold_source_org_raw="", gold_source_role="미상",
    gold_source_evidence_quote="해당 기간 상승 폭은 12.3%포인트로, 미국(15.8%p)에 이어 둘째로 높았다.",
    gold_notes="한계기업 비율 국제 비교 통계, 출처 불명확.")

CD["C007"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="외국인 주민 수", gold_population="국내 장기체류 외국인(3개월 초과)",
    gold_value="", gold_unit="명", gold_time_ref="2024년 11월 1일",
    gold_source_org_raw="국가데이터처(행안부)", gold_source_role="발표기관",
    gold_source_evidence_quote="집계 대상은 2024년 11월 1일 기준, 3개월을 초과해 국내에 장기 거주한 외국인 주민의 수다.",
    gold_notes="집계 대상 정의 문장으로 구체 값(258만명)은 제목·타 문장에 있음.")

CD["C008"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="20대 인구 규모", gold_population="20대",
    gold_value="", gold_unit="",
    gold_source_org_raw="국가데이터처(추정)", gold_source_role="미상",
    gold_source_evidence_quote="한때 성인 가운데 가장 많았던 20대가 4년 연속 줄어들며 이제는 가장 적은 세대가 됐다.",
    gold_notes="20대 인구 4년 연속 감소·최소 세대 전환. 구체 값 없음.")

CD["C011"] = YES(gold_source_scope="민간기관", gold_verifiability_prefilter="False",
    gold_indicator_raw="원룸 평균 월세", gold_population="원룸",
    gold_value="2.4", gold_unit="%", gold_time_compare="전월",
    gold_source_org_raw="", gold_source_role="미상(부동산 플랫폼 추정)",
    gold_source_evidence_quote="전월 대비 2.4%(2만원) 올랐다.",
    gold_notes="원룸 평균 월세(기사상 102만원) 전월比 2.4% 상승, 민간 데이터.")

CD["C021"] = YES(gold_source_scope="민간기관", gold_verifiability_prefilter="False",
    gold_indicator_raw="은행 임직원 성별 평균 연봉", gold_population="은행 임직원",
    gold_value="남 1억3475만; 여 1억450만", gold_unit="원",
    gold_source_org_raw="", gold_source_role="미상",
    gold_source_evidence_quote="남성 임직원의 평균 연봉은 1억3475만원으로 여성 임직원(1억450만원)보다 3025만원 더 많았다.",
    gold_notes="성별 평균 연봉 격차 3025만원.")

CD["C033"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="소비자물가 농축수산물 기여도", gold_population="전국",
    gold_value="0.25", gold_unit="%p", gold_time_ref="2025년 10월",
    gold_source_org_raw="통계청(국가데이터처)", gold_source_role="발표기관",
    gold_source_evidence_quote="소비자물가 상승률 중 농축수산물의 기여도는 0.25%p로 다른 항목에 비해 낮았으나",
    gold_notes="10월 소비자물가 농축수산물 기여도.")

CD["C034"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="취업자 수 증감(전년동기대비)", gold_population="전국",
    gold_value="-5만2000", gold_unit="명", gold_time_ref="지난달", gold_time_compare="전년 동기",
    gold_source_org_raw="통계청", gold_source_role="발표기관",
    gold_source_evidence_quote="지난달 취업자 수가 46개월 만에 5만2000명(전년 동기 대비) 감소 전환한 데 따른 조치다.",
    gold_notes="46개월 만의 취업자 감소 전환.")

CD["C036"] = YES(gold_source_scope="KOSIS계열", gold_verifiability_prefilter="True",
    gold_indicator_raw="산업별 수출액(광제조업/전기전자/운송장비)", gold_population="전국",
    gold_value="광제조업 1595억달러(+8.0); 전기전자 +15.0; 운송장비 +9.3", gold_unit="달러/%",
    gold_source_org_raw="산업통상자원부·무역협회(추정)", gold_source_role="발표기관",
    gold_source_evidence_quote="광제조업이 1595억달러로 8.0% 늘었고, 전기전자(15.0%)와 운송장비(9.3%) 수출이 크게 증가했다.",
    gold_notes="산업별 수출 증감.")

# False 후보
CD["C002"] = NO("영풍의 고려아연 지분 현물출자(개별 기업 거래·지분율).")
CD["C003"] = NO("HD현대일렉트릭 투자·증설 계획(개별 기업).")
CD["C009"] = NO("100만원 미만 저임금 일자리 확대라는 정성적 서술(임금 구간 언급).")
CD["C010"] = NO("테슬라 주가(특정 시점 시장 시세).")
CD["C012"] = NO("한화운용 ETF 점유율 순위(개별 운용사).")
CD["C013"] = NO("소득공제 연간 한도 300만원(제도 기준).")
CD["C014"] = NO("기아 미국 판매량(개별 기업 실적).")
CD["C015"] = NO("원·달러 환율 시세(특정 시점).")
CD["C016"] = NO("북한 선원 개인 증언.")
CD["C017"] = NO("영국 상호 관세율 조율(정책·협상).")
CD["C018"] = NO("'14억 인도인' 수사적 표현·세계 1위 순위.")
CD["C019"] = NO("미국 수입차 시장 2위·관세율 25%(순위·정책).")
CD["C020"] = NO("김수현 인기 관련 개인·연도.")
CD["C022"] = NO("'육체적으로 힘들어서 47%' 설문조사 결과.")
CD["C023"] = NO("로또 2등 당첨자 80명·당첨금(개별 사행성 결과).")
CD["C024"] = NO("지하철 요금 150원 인상(정책 계획).")
CD["C025"] = NO("2025~2030 소비 둔화폭 연 1%p 확대 전망(예측).")
CD["C026"] = NO("카이즈유 1분기 전기차 판매 순위(개별 조사 순위).")
CD["C027"] = NO("김범수 기부 이력(개인).")
CD["C028"] = NO("엑손모빌·셰브런 주가(시장 시세).")
CD["C029"] = NO("삼성전자 2분기 영업이익(개별 기업 실적).")
CD["C030"] = NO("동영상 플레이어 UI 텍스트(노이즈).")
CD["C031"] = NO("코친조선소 인도 정부 지분율(지분).")
CD["C032"] = NO("자막 설정 UI 텍스트(노이즈).")
CD["C035"] = NO("제주항공 캐릭터 협업 기간(개별 기업).")
CD["C037"] = NO("개표율·득표율(선거 개표 결과).")
CD["C038"] = NO("S&P500·나스닥 지수(특정 시점 시장 시세).")
CD["C039"] = NO("공장 증설·점유율 1위 계획(계획·순위).")
CD["C040"] = NO("방통위 예산 삭감액(개별 기관 예산).")
CD["C041"] = NO("쿠팡 반품 사기 편취액(개별 사건).")
CD["C042"] = NO("두봉 주교 출생연도(개인 신상).")
CD["C043"] = NO("미·중 관세율 인하(관세율 정책).")
CD["C044"] = NO("미국 화장품 수입액 1위(수치 없는 순위).")
CD["C045"] = NO("항셍·상해종합 지수(시장 시세).")
CD["C046"] = NO("신한울 3·4호기 인허가 소요 기간(사건 경과).")
CD["C047"] = NO("보잉 합병·대량 해고 등 역사적 개별 기업 서술.")
CD["C048"] = NO("물가 1%대 회복·경기 회복 전망 서술(전망).")
CD["C049"] = NO("로봇 밀도 세계 1위(수치 없는 순위).")
CD["C050"] = NO("테슬라 모델 판매 순위(개별 모델).")


# =====================================================================
# write
# =====================================================================
def write_article(path, data):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
    n = 0
    for row in rows:
        sid = row["sample_id"]
        if sid not in data:
            continue
        claims, notes = data[sid]
        row["gold_annotation_complete"] = "TRUE"
        row["gold_claim_count"] = str(len(claims))
        row["gold_claims_json"] = json.dumps(claims, ensure_ascii=False)
        row["gold_notes"] = notes
        row["annotator"] = ANNOTATOR
        n += 1
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tot = sum(len(data[s][0]) for s in data)
    print(f"[article] filled {n}/{len(rows)}, articles>=1claim {sum(1 for s in data if data[s][0])}, total claims {tot}")


def write_candidate(path, data):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
    n = 0
    for row in rows:
        sid = row["sample_id"]
        if sid not in data:
            continue
        vals = data[sid]
        for col in CAND_COLS:
            row[col] = vals.get(col, "")
        n += 1
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    nt = sum(1 for s in data if data[s].get("gold_is_aggregate_claim") == "True")
    print(f"[candidate] filled {n}/{len(rows)}, aggregate True {nt}, False {len(data)-nt}")


write_article(ART, A)
write_candidate(CAND, CD)
