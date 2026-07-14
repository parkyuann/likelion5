# -*- coding: utf-8 -*-
"""article_labeling_pilot_relaxed.csv 의 gold_* 5개 컬럼을 채우는 스크립트.

라벨링 가이드에 따라 각 기사 본문에서 '집계통계 주장'만 gold_claims_json 으로 기록한다.
제외 대상: 개인 신상/나이, 단순 날짜, 전망·예측, 목표·계획, 법령/제도 기준,
여론조사, 사고 현장 임시 집계, 수치 없는 평가, 개별 기업·기금 실적 수치.
"""
import csv
import json
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "labeling" / "article_labeling_pilot_relaxed.csv"
ANNOTATOR = "claude"

# sample_id -> (claims, notes)
DATA = {}

def C(**kw):
    # 빈 값 컬럼은 제거해 JSON 을 깔끔하게 유지
    return {k: v for k, v in kw.items() if v not in ("", None)}

# ---------------------------------------------------------------- A001
DATA["A001"] = ([
    C(claim_text="미국의 2024년 12월 비농업 일자리가 전월보다 25만6000개 늘었다.",
      evidence_quote="미국의 지난해 12월 비농업 일자리가 전월보다 25만6000개 늘었다고 미 노동부가 10일 밝혔다.",
      claim_class="집계통계", indicator_raw="비농업 일자리(취업자)", population="미국 전체",
      value="256000", unit="개", change_value="256000", change_type="증감폭",
      time_ref="2024년 12월", time_compare="전월(11월)",
      source_org_raw="미 노동부", source_scope="해외기관"),
    C(claim_text="미국의 2024년 12월 실업률은 4.1%로 전월(4.2%)보다 0.1%포인트 낮아졌다.",
      evidence_quote="실업률은 전월(4.2%)보다 소폭(0.1% 포인트) 떨어진 4.1%를 기록했다.",
      claim_class="집계통계", indicator_raw="실업률", population="미국 전체",
      value="4.1", unit="%", change_value="0.1", change_unit="%p", change_type="증감폭",
      time_ref="2024년 12월", time_compare="전월(11월)",
      source_org_raw="미 노동부", source_scope="해외기관"),
], "전문가 예상치(15만5000개)는 전망이라 제외, 국채 10년물 금리·달러인덱스는 특정 시점 시장 시세라 제외.")

# ---------------------------------------------------------------- A002
DATA["A002"] = ([
    C(claim_text="2025년 1월 3일 기준 배추 평균 소매가격은 한 포기 5027원으로 1년 전보다 59% 올랐다.",
      evidence_quote="지난 3일 기준 배추의 평균 소매가격은 한 포기에 5027원으로 1년 전, 평년과 비교해 각각 59%, 42% 올랐다.",
      claim_class="집계통계", indicator_raw="배추 소매가격", population="전국",
      value="5027", unit="원", change_value="59", change_unit="%", change_type="증감률",
      time_ref="2025년 1월 3일", time_compare="1년 전",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
    C(claim_text="무 평균 소매가격은 한 개 3206원으로 1년 전보다 77% 비싸졌다.",
      evidence_quote="무는 한 개에 3206원으로 1년 전보다 77% 비싸졌고, 평년보다 53% 가격이 올랐다.",
      claim_class="집계통계", indicator_raw="무 소매가격", population="전국",
      value="3206", unit="원", change_value="77", change_unit="%", change_type="증감률",
      time_ref="2025년 1월 3일", time_compare="1년 전",
      source_org_raw="한국농수산식품유통공사(aT)", source_scope="국내공공기관"),
    C(claim_text="2024년 여름철(6~8월) 평균 기온이 25.6도로 1973년 이후 최고치를 기록했다.",
      evidence_quote="지난해 여름철(6~8월) 평균 기온이 25.6°C로 1973년 이후 최고치를 기록한 데다",
      claim_class="집계통계", indicator_raw="여름철 평균기온", population="전국",
      value="25.6", unit="℃", time_ref="2024년 여름철(6~8월)", time_compare="1973년 이후",
      source_org_raw="", source_scope="KOSIS계열"),
], "여름철 평균기온은 출처 명시 없으나 기상 집계통계로 판단. 정부 수급 대책은 계획이라 제외.")

# ---------------------------------------------------------------- A003
DATA["A003"] = ([], "현대차 개별 기업의 매출·영업이익·판매량 수치로 모집단 집계통계 주장이 아님. 관세로 인한 영업이익 감소 전망도 예측이라 제외.")

# ---------------------------------------------------------------- A004
DATA["A004"] = ([], "배우 나이·결혼 기간 등 개인 신상과 날짜만 존재. 집계통계 주장 없음.")

# ---------------------------------------------------------------- A005
DATA["A005"] = ([
    C(claim_text="2025년 10월 소비자물가가 15개월 만에 가장 큰 폭으로 상승했다.",
      evidence_quote="10월 소비자물가가 15개월 만에 가장 큰 폭으로 상승했다.",
      claim_class="집계통계", indicator_raw="소비자물가 상승률", population="전국",
      time_ref="2025년 10월", time_compare="15개월 전",
      source_org_raw="", source_scope="KOSIS계열"),
], "본문(요약)에 구체적 상승률 수치는 없으나 CPI 관련 집계통계 주장이라 기록. value 공란.")

# ---------------------------------------------------------------- A006
DATA["A006"] = ([], "한미 정상 통화 시각·시간 외 수치 없음. 집계통계 주장 없음.")

# ---------------------------------------------------------------- A007
DATA["A007"] = ([
    C(claim_text="지난달 원/달러 평균 환율은 1434.2원으로 전달(1393.38원)보다 2.9% 올랐다.",
      evidence_quote="지난달 미 달러화 대비 원화 평균 환율은 1434.2원을 기록해 전달(1393.38원)대비 2.9% 가량 올랐다.",
      claim_class="집계통계", indicator_raw="원/달러 월평균 환율",
      value="1434.2", unit="원", change_value="2.9", change_unit="%", change_type="증감률",
      time_ref="지난달(2024년 12월)", time_compare="전달(11월)",
      source_org_raw="한국은행", source_scope="KOSIS계열"),
], "기준금리 3% 동결은 정책 결정이라 제외, 현재 1460~1470원대는 시점 시세라 제외, 설문조사(동결60/인하40)는 여론조사라 제외.")

# ---------------------------------------------------------------- A008
DATA["A008"] = ([], "챗GPT 오답 사례 기사로 수치 기반 집계통계 주장 없음.")

# ---------------------------------------------------------------- A009
DATA["A009"] = ([], "서울우유 평균 7.5% 인상 등은 개별 기업의 가격 인상 조치이고, 코코아 3배·원두 2배 등은 업체 주장이라 집계통계 주장으로 보지 않음.")

# ---------------------------------------------------------------- A010
DATA["A010"] = ([], "가정폭력 사건의 부상자 수·나이는 사고 현장 임시 집계와 개인 정보라 제외.")

# ---------------------------------------------------------------- A011
DATA["A011"] = ([], "국민연금 등 개별 기금의 수익률·적립금·수익금 수치로 모집단 대상 집계통계 주장이 아님(개별 기관 실적).")

# ---------------------------------------------------------------- A012
DATA["A012"] = ([], "과징금·가맹금률·소송 금액 등 개별 사건의 행정·법적 수치라 집계통계 주장 아님.")

# ---------------------------------------------------------------- A013
DATA["A013"] = ([
    C(claim_text="국세청이 고액·상습 체납자 710명에 대한 재산 추적 조사에 착수했으며 체납 규모는 1조원대다.",
      evidence_quote="고액·상습 체납자 710명에 대한 재산 추적 조사에 최근 착수했다",
      claim_class="집계통계", indicator_raw="고액·상습 체납자 수", population="전국",
      value="710", unit="명", time_ref="2025년",
      source_org_raw="국세청", source_scope="국내정부기관"),
    C(claim_text="국세청은 지난해 체납자로부터 2조8000억원을 징수했다.",
      evidence_quote="지난해 체납자가 숨긴 재산을 압류하기 위해 2064회의 현장 수색...2조8000억원을 징수했다고 밝혔다.",
      claim_class="집계통계", indicator_raw="체납 징수액", population="전국",
      value="2조8000억", unit="원", time_ref="지난해(2024년)",
      source_org_raw="국세청", source_scope="국내정부기관"),
], "개별 체납 사례(A·B·C씨) 금액은 개인 사례라 제외. 710명·2조8000억원은 국세청 연간 집계라 기록.")

# ---------------------------------------------------------------- A014
DATA["A014"] = ([], "포켓몬 행사 홍보 기사로 집계통계 주장 없음.")

# ---------------------------------------------------------------- A015
DATA["A015"] = ([
    C(claim_text="2025년 5월 기준 한국이 수입한 미국산 소고기는 2만5228t으로 미국의 전 세계 수출 물량(9만7266t)의 25.9%에 달한다.",
      evidence_quote="지난 5월 기준 한국이 수입한 미국산 소고기는 2만5228t으로 미국의 전 세계 수출 물량(9만7266t)의 25.9%에 달한다.",
      claim_class="집계통계", indicator_raw="미국산 소고기 수입량(한국)", population="한국",
      value="25228", unit="t", change_value="25.9", change_unit="%", change_type="비중",
      time_ref="2025년 5월",
      source_org_raw="전미육류수출협회", source_scope="해외기관"),
], "검역 8단계·1993년 사과 수입 신청 등은 제도·이력이라 제외. 협상 카드·전망은 계획/예측이라 제외.")

# ---------------------------------------------------------------- A016
DATA["A016"] = ([], "내년 최저임금 1만320원(2.9%↑)·환산 월급은 정책 결정 수치라 집계통계 주장 아님. 17년 만의 합의, 위원 퇴장 등은 사실 기록.")

# ---------------------------------------------------------------- A017
DATA["A017"] = ([
    C(claim_text="미국의 2025년 1월 소비자물가지수가 전년 동월 대비 3% 상승했다.",
      evidence_quote="미국의 1월 소비자물가지수가 전년 동월 대비 3% 상승했다고 미 노동부가 12일 밝혔다.",
      claim_class="집계통계", indicator_raw="소비자물가지수(CPI)", population="미국 전체",
      value="3", unit="%", change_type="증감률", time_ref="2025년 1월", time_compare="전년 동월",
      source_org_raw="미 노동부", source_scope="해외기관"),
    C(claim_text="미국의 2025년 1월 근원 인플레이션은 3.3%로 나타났다.",
      evidence_quote="변동성이 큰 에너지와 식품을 뺀 근원 인플레이션은 3.3%로 나타났다.",
      claim_class="집계통계", indicator_raw="근원 인플레이션", population="미국 전체",
      value="3.3", unit="%", change_type="증감률", time_ref="2025년 1월", time_compare="전년 동월",
      source_org_raw="미 노동부", source_scope="해외기관"),
], "예상치(2.9%)·페드워치 금리 동결 확률(65%)은 전망/시장 확률이라 제외.")

# ---------------------------------------------------------------- A018
DATA["A018"] = ([], "협회 간 업무협약 기사로 집계통계 주장 없음.")

# ---------------------------------------------------------------- A019
DATA["A019"] = ([
    C(claim_text="이번 산불로 국내 사과 재배 면적의 9%인 3000ha에서 피해 신고가 접수됐다.",
      evidence_quote="이번 산불로 국내 사과 재배 면적의 9%인 3000ha에서 피해 신고가 들어왔다.",
      claim_class="집계통계", indicator_raw="사과 재배면적 대비 피해 신고 비율", population="전국",
      value="9", unit="%", time_ref="2025년",
      source_org_raw="농림축산식품부", source_scope="국내정부기관"),
], "생계비·학자금·4000억원 지원 및 대출 조건은 정책 계획이라 제외. 사과 재배면적의 9% 피해는 재배면적 통계에 근거해 기록(피해 신고 기반이라 다소 애매).")

# ---------------------------------------------------------------- A020
DATA["A020"] = ([
    C(claim_text="미국의 2025년 2월 비농업 일자리가 전월보다 15만1000개 늘었다.",
      evidence_quote="미국의 2월 비농업 일자리가 전월보다 15만1000개 늘어났다고 미 노동부가 7일 밝혔다.",
      claim_class="집계통계", indicator_raw="비농업 일자리(취업자)", population="미국 전체",
      value="151000", unit="개", change_value="151000", change_type="증감폭",
      time_ref="2025년 2월", time_compare="전월(1월)",
      source_org_raw="미 노동부", source_scope="해외기관"),
    C(claim_text="미국의 2025년 2월 실업률은 4.1%로 전월(4%)보다 높아졌다.",
      evidence_quote="실업률 또한 4.1%로 전월(4%)보다 높아졌다.",
      claim_class="집계통계", indicator_raw="실업률", population="미국 전체",
      value="4.1", unit="%", change_type="증감폭", time_ref="2025년 2월", time_compare="전월(1월)",
      source_org_raw="미 노동부", source_scope="해외기관"),
    C(claim_text="미국의 2025년 1월 개인소비지출(PCE)이 전월 대비 0.2% 감소했다.",
      evidence_quote="지난달 말 발표된 1월 개인소비지출(PCE)은 전월 대비 0.2% 감소했다.",
      claim_class="집계통계", indicator_raw="개인소비지출(PCE)", population="미국 전체",
      value="0.2", unit="%", change_type="증감률(감소)", time_ref="2025년 1월", time_compare="전월",
      source_org_raw="", source_scope="해외기관"),
], "전문가 예상치(15만9000개)·페드워치 확률은 전망/시장 확률이라 제외.")

# ---------------------------------------------------------------- A021
DATA["A021"] = ([], "주가조작 수사 관련 개별 사건 정황·통화 시간 등으로 집계통계 주장 없음.")

# ---------------------------------------------------------------- A022
DATA["A022"] = ([], "공매도 규제 기준(잔고 0.01%/10억원, 5년 보관, 연 1회 점검 등)은 제도상 기준 수치라 집계통계 주장 아님.")

# ---------------------------------------------------------------- A023
DATA["A023"] = ([], "비만치료제 월 약값 1000달러는 개별 상품 가격이라 집계통계 주장 아님. 보험 적용 철회는 정책 결정.")

# ---------------------------------------------------------------- A024
DATA["A024"] = ([], "북한 선원 착취 관련 증언·개별 사례(8년, 330달러 등)라 집계통계 주장 아님.")

# ---------------------------------------------------------------- A025
DATA["A025"] = ([], "은행별 대출 규제 도입 조치·일정으로 집계통계 주장 없음.")

# ---------------------------------------------------------------- A026
DATA["A026"] = ([], "온누리상품권 환급 행사 조건(30% 환급, 2만원 한도, 84곳)은 행사 기준 수치라 집계통계 주장 아님.")

# ---------------------------------------------------------------- A027
DATA["A027"] = ([
    C(claim_text="2025년 1~3월 김치 수입액은 4756만달러로 작년 같은 기간보다 16.7% 늘었다.",
      evidence_quote="올해 1∼3월 김치 수입액은 4756만달러(약 670억원)로 작년 같은 기간보다 16.7% 늘었다.",
      claim_class="집계통계", indicator_raw="김치 수입액", population="전국",
      value="4756만", unit="달러", change_value="16.7", change_unit="%", change_type="증감률",
      time_ref="2025년 1~3월", time_compare="작년 동기",
      source_org_raw="관세청", source_scope="KOSIS계열"),
], "")

# ---------------------------------------------------------------- A028
DATA["A028"] = ([], "쌀 시장격리 '3%·5% 룰'은 제도상 발동 기준 수치라 집계통계 주장 아님.")

# ---------------------------------------------------------------- A029
DATA["A029"] = ([
    C(claim_text="올해 3분기 한국 경제가 1.2% 성장했다.",
      evidence_quote="올해 3분기(7~9월) 1.2%의 '깜짝' 성장을 할 수 있었던 것도",
      claim_class="집계통계", indicator_raw="실질 GDP 성장률", population="한국",
      value="1.2", unit="%", change_type="증감률", time_ref="2025년 3분기",
      source_org_raw="한국은행", source_scope="KOSIS계열"),
    C(claim_text="2025년 9월 설비 투자가 전월보다 12.7% 급증했다.",
      evidence_quote="지난달 기업들 설비 투자는 전월보다 12.7% 급증했다.",
      claim_class="집계통계", indicator_raw="설비투자", population="전국",
      value="12.7", unit="%", change_type="증감률", time_ref="2025년 9월", time_compare="전월",
      source_org_raw="국가데이터처", source_scope="KOSIS계열"),
    C(claim_text="2025년 9월 반도체 생산이 전월 대비 19.6% 증가해 2023년 3월 이후 최대 폭으로 늘었다.",
      evidence_quote="지난달 반도체 생산도 전월 대비 19.6% 증가하며, 2023년 3월 이후 2년 6개월 만에 가장 크게 늘었다.",
      claim_class="집계통계", indicator_raw="반도체 생산", population="전국",
      value="19.6", unit="%", change_type="증감률", time_ref="2025년 9월", time_compare="전월",
      source_org_raw="국가데이터처", source_scope="KOSIS계열"),
    C(claim_text="2025년 9월 건설 기성이 11.4% 늘어 1년 8개월 만에 가장 크게 증가했다.",
      evidence_quote="건설 기성(시공한 실적)은 11.4% 늘면서 지난해 1월 이후 1년 8개월 만에 가장 크게 증가했다.",
      claim_class="집계통계", indicator_raw="건설기성", population="전국",
      value="11.4", unit="%", change_type="증감률", time_ref="2025년 9월",
      source_org_raw="국가데이터처", source_scope="KOSIS계열"),
    C(claim_text="올해 3분기 민간 소비가 1.3% 증가했다.",
      evidence_quote="실제 3분기 민간 소비는 1.3% 증가했다.",
      claim_class="집계통계", indicator_raw="민간소비", population="한국",
      value="1.3", unit="%", change_type="증감률", time_ref="2025년 3분기",
      source_org_raw="한국은행", source_scope="KOSIS계열"),
    C(claim_text="소매 판매가 7월 전월보다 2.7% 늘었으나 8월 -2.4%, 9월 -0.1%로 두 달 연속 감소했다.",
      evidence_quote="7월에는 전월보다 2.7%나 늘었지만, 8·9월에는 각각 2.4%, 0.1% 줄면서 두 달 연속 마이너스(-)를 기록했다.",
      claim_class="집계통계", indicator_raw="소매판매", population="전국",
      value="-0.1", unit="%", change_type="증감률", time_ref="2025년 9월", time_compare="전월",
      source_org_raw="국가데이터처", source_scope="KOSIS계열"),
    C(claim_text="2025년 3분기 제조업 평균 가동률은 73.5%로 2분기(72.5%)보다 1%포인트 높다.",
      evidence_quote="3분기 제조업 평균 가동률은 73.5%로 2분기(72.5%)보다 1%포인트 높다.",
      claim_class="집계통계", indicator_raw="제조업 평균 가동률", population="전국",
      value="73.5", unit="%", change_value="1", change_unit="%p", change_type="증감폭",
      time_ref="2025년 3분기", time_compare="2분기",
      source_org_raw="국가데이터처", source_scope="KOSIS계열"),
    C(claim_text="2025년 9월 자동차 생산이 전월 대비 18.3% 급감했다.",
      evidence_quote="지난달 자동차 생산은 18.3% 급감했는데",
      claim_class="집계통계", indicator_raw="자동차 생산", population="전국",
      value="18.3", unit="%", change_type="증감률(감소)", time_ref="2025년 9월", time_compare="전월",
      source_org_raw="국가데이터처", source_scope="KOSIS계열"),
    C(claim_text="올해 6월 전 세계 반도체 매출은 약 599억달러로 지난해보다 19.6% 증가했다.",
      evidence_quote="올해 6월 전 세계 반도체 매출은 약 599억달러(약 85조 5000억원)로 지난해보다 19.6% 증가했다.",
      claim_class="집계통계", indicator_raw="세계 반도체 매출", population="전 세계",
      value="599억", unit="달러", change_value="19.6", change_unit="%", change_type="증감률",
      time_ref="2025년 6월", time_compare="전년",
      source_org_raw="미국 반도체산업협회(SIA)", source_scope="해외기관"),
], "이창용 총재의 1%대 성장 전망은 예측이라 제외.")

# ---------------------------------------------------------------- A030
DATA["A030"] = ([], "제4인터넷은행 참여 포기 등 개별 기업 의사결정 기사로 집계통계 주장 없음.")

# ================================================================ write
with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    rows = list(reader)

filled = 0
for row in rows:
    sid = row["sample_id"]
    if sid not in DATA:
        continue
    claims, notes = DATA[sid]
    row["gold_annotation_complete"] = "TRUE"
    row["gold_claim_count"] = str(len(claims))
    row["gold_claims_json"] = json.dumps(claims, ensure_ascii=False)
    row["gold_notes"] = notes
    row["annotator"] = ANNOTATOR
    filled += 1

with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

total_claims = sum(len(DATA[s][0]) for s in DATA)
print(f"filled rows: {filled} / {len(rows)}")
print(f"articles with >=1 claim: {sum(1 for s in DATA if DATA[s][0])}")
print(f"total gold claims: {total_claims}")
