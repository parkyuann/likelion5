"""
축 B — KOSIS 표별 통계적 정확도 등급 룩업.

여기서 "등급"은 그 표의 공식 수치가 조사설계상 원래 얼마나 "정확한" 성격인지를 나타낸다
(전수조사=표본오차 없음, 확률표본조사=공식 목표오차 있음, 유의표본추출=오차수치 미공표).
뉴스 표현이 실제 값을 얼마나 근사해서 썼는지를 다루는 축 A(tolerance_judge.py의
ExpressionType)와는 별개 문제다 — 자세한 설명은 tolerance_rules.md 참고.

각 항목은 k-stat.go.kr(국가데이터처, 구 통계청) "통계별설명자료조회"
(https://www.k-stat.go.kr/metasvc/msea100/statsdcdta-popup?statsConfmNo=<승인번호>)를
실제로 열람해 확인한 내용만 근거로 삼는다 확인하지 못한 표는 임의로 등급을 추정하지 않고 
룩업에서아예 빼서, get_grade()가 None을 반환하면 호출측(tolerance_judge)이 NEEDS_REVIEW로
처리하도록 한다.
"""

from dataclasses import dataclass
from enum import Enum


class StatGrade(Enum):
    CENSUS_ADMIN = "census_admin"              # 전수, 행정자료(신고) 집계 — 표본오차 없음
    SAMPLE_PROBABILITY = "sample_probability"  # 확률표본조사 — 공식 목표 상대표준오차(RSE) 있음
    SAMPLE_PURPOSIVE = "sample_purposive"      # 유의표본추출(비확률) — 공식 RSE 미공표
    MIXED = "mixed"                            # 표본조사+행정자료 혼합(항목별로 상이)


@dataclass(frozen=True)
class StatisticalGrade:
    table_key: str                  # kosis_client 관례와 동일한 "orgId:tblId" 형식
    survey_name: str
    stats_confm_no: str              # 통계 승인번호(k-stat.go.kr 조회 키)
    grade: StatGrade
    target_rse_pct: float | None     # 공식 목표 상대표준오차(%). 없으면 None
    source_note: str                 # 실제 열람한 원문 인용 + 조회 URL


_GRADES: dict[str, StatisticalGrade] = {
    "101:DT_1DE7110_11": StatisticalGrade(
        table_key="101:DT_1DE7110_11",
        survey_name="경제활동인구조사(근로형태별 부가조사) — 비정규직 근로자 규모·비중",
        stats_confm_no="101004",
        grade=StatGrade.SAMPLE_PROBABILITY,
        target_rse_pct=2.0,
        source_note=(
            "표본설계 원문: '표본배분: 실업자수의 연간 상대표준오차를 기준으로 전국 2%, "
            "시도 5~15%이내를 목표오차로 하여 시도별로 표본조사구 배분'. 통계종류=지정통계, "
            "작성유형=조사통계, 추출방법=층화 2단 집락추출(확률표본). 조회: "
            "https://www.k-stat.go.kr/metasvc/msea100/statsdcdta-popup?statsConfmNo=101004"
        ),
    ),
    "101:DT_1DA7102S": StatisticalGrade(
        table_key="101:DT_1DA7102S",
        survey_name="경제활동인구조사 — 성/연령별 실업률",
        stats_confm_no="101004",
        grade=StatGrade.SAMPLE_PROBABILITY,
        target_rse_pct=2.0,
        source_note="DT_1DE7110_11과 동일 모조사(경제활동인구조사, 승인번호 101004)에서 근거 재사용.",
    ),
    "101:DT_1DA7024S": StatisticalGrade(
        table_key="101:DT_1DA7024S",
        survey_name="경제활동인구조사 — 성/연령별 취업자",
        stats_confm_no="101004",
        grade=StatGrade.SAMPLE_PROBABILITY,
        target_rse_pct=2.0,
        source_note="DT_1DE7110_11과 동일 모조사(경제활동인구조사, 승인번호 101004)에서 근거 재사용.",
    ),
    "101:DT_1B8000G": StatisticalGrade(
        table_key="101:DT_1B8000G",
        survey_name="인구동향조사(출생·사망·혼인·이혼)",
        stats_confm_no="101003",
        grade=StatGrade.CENSUS_ADMIN,
        target_rse_pct=None,
        source_note=(
            "원문: 자료수집방법='(주) 기타', 조사체계='신고인 ▶ 읍면동(가족관계등록계) ▶ "
            "시군구 ▶ 시도 ▶ 국가데이터처' — 표본추출 절차 없이 신고 전수를 그대로 집계하는 "
            "행정자료 기반 통계이며, 페이지에 표본설계/표본오차 섹션 자체가 없음(전수라 해당 "
            "없음). 통계종류=지정통계. 조회: "
            "https://www.k-stat.go.kr/metasvc/msea100/statsdcdta-popup?statsConfmNo=101003"
        ),
    ),
    "101:DT_1J22003": StatisticalGrade(
        table_key="101:DT_1J22003",
        survey_name="소비자물가지수(2020=100)",
        stats_confm_no="101007",
        grade=StatGrade.SAMPLE_PURPOSIVE,
        target_rse_pct=None,
        source_note=(
            "원문: 표본추출방법='유의표본추출' — 확률표본이 아니라 상권·매출 규모 등을 고려한 "
            "유의추출이라, 경제활동인구조사처럼 공식 목표 RSE 수치가 공표되지 않음(표본설계 "
            "섹션에 RSE 언급 없음). 통계종류=지정통계, 작성유형=조사통계. 조회: "
            "https://www.k-stat.go.kr/metasvc/msea100/statsdcdta-popup?statsConfmNo=101007"
        ),
    ),
    "101:DT_1ET0021": StatisticalGrade(
        table_key="101:DT_1ET0021",
        survey_name="농작물생산조사(식량작물 생산량)",
        stats_confm_no="114004",
        grade=StatGrade.MIXED,
        target_rse_pct=None,
        source_note=(
            "원문: 조사체계='표본조사 작물(16종): 지방청(사무소)▶국가데이터처 / 행정조사 "
            "작물(37종): 읍면동▶시군▶시도▶농림축산식품부' — 작물마다 표본조사(실측)와 "
            "행정조사(지자체 보고 전수)가 갈린다. 미곡(쌀) 등 구체적으로 어느 작물이 표본조사 "
            "16종에 포함되는지는 이번 조회로 특정하지 못했다 — 항목별 재확인 전까지는 "
            "이 표 전체를 NEEDS_REVIEW에 준해 다루는 것을 권장. 조회: "
            "https://www.k-stat.go.kr/metasvc/msea100/statsdcdta-popup?statsConfmNo=114004"
        ),
    ),
}

# 확인했지만 상세 수치를 특정하지 못한 표(참고용 — 룩업에는 넣지 않음, get_grade()가
# None을 반환해 NEEDS_REVIEW로 자연스럽게 떨어지도록 둔다):
# - 134:DT_134001_001 (수출입총괄, 관세청): statsConfmNo=134001 조회는 됐으나 표본설계
#   섹션이 비어 있어 "전수(통관신고 기반)"라는 통상적 이해를 공식 문서로 확인하지 못함.
# - 408:DT_30404_B012 (유형별 매매가격지수, 한국부동산원), 301:DT_200Y102(국민계정,
#   한국은행), 101:DT_1EI10122/DT_1EI10179(산지쌀값조사): 이번 세션에서 statsConfmNo을
#   확인/조회하지 못함 — 실전2 매핑 확장 시 같은 방식(k-stat.go.kr statsdcdta-popup)으로
#   추가 조사 후 이 딕셔너리에 채워 넣는다.


def get_grade(table_key: str) -> StatisticalGrade | None:
    """table_key는 'orgId:tblId' 형식(kosis_client._table_key와 동일 관례).

    룩업에 없으면 None을 반환한다 — 임의로 등급을 추정하지 않고, 호출측이 이를
    NEEDS_REVIEW로 처리하도록 맡긴다.
    """
    return _GRADES.get(table_key)


# ---------------------------------------------------------------------------
# 축 C — 개정형(잠정→확정) 통계 레지스트리.
#
# 이 통계들은 발표 시 잠정치를 내고 이후 확정치로 값을 개정한다. 뉴스는 발행 시점의
# 잠정치를 정확히 인용했을 수 있으므로, 현재(확정)값과 반올림을 넘는 차이가 나도 곧바로
# '불일치'로 보지 않고 tolerance_judge가 '개정상이(REVISED_DIFF)'로 라벨한다.
# 근거: 발표기관 보도자료 제목의 "(잠정)" 표기 + 라이브 확인(예: 수입물가 142.14→141.98).
# 확장은 각 표의 통계정보보고서 "자료의 개정" 섹션으로 확인해 추가한다.
# ---------------------------------------------------------------------------

_REVISION_PRONE: set[str] = {
    "101:DT_1B8000G",       # 인구동향(출생/사망/혼인/이혼) — 잠정→확정
    "134:DT_134001_001",    # 수출입총괄(관세청) — 속보→확정
    "101:DT_1JH20201",      # 전산업생산지수(산업활동동향) — 잠정→확정
    "101:DT_1JH20202",      # 전산업생산지수(계절조정) — 잠정→확정
    "301:DT_401Y015",       # 수입물가지수(한은) — 발표 전부 "(잠정)", 확정 개정
    "301:DT_403Y001",       # 수출물가지수(한은) — 동일
}

# 개정으로 볼 수 있는 최대 상대오차(%). 이보다 크면 개정이 아니라 진짜 불일치(틀린 표/값)로 본다.
# 프로젝트 설계값(공식 기준 아님) — 잠정↔확정 개정폭 실측(출생 0.06%·수입물가 0.11%·수출 0.03%) 기반.
REVISION_BAND_REL_PCT = 3.0


def is_revision_prone(table_key: str | None) -> bool:
    """이 표가 잠정→확정 개정형인지. tolerance_judge 축C에서 사용."""
    return table_key in _REVISION_PRONE
