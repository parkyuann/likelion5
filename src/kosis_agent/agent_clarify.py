"""agent_clarify.py — 실전2 재질의(Clarification) 판단 모듈.

매핑(하이브리드 검색)이 표를 찾아준 뒤, KOSIS `get_data` 호출에 필요한 슬롯
(항목 itmId / 분류 objL1.. / 시점 prdSe)이 다 찼는지 보고 슬롯마다 다음 넷 중
하나를 결정한다.

  FILLED   질의에서 값이 정확히 하나로 확정됨 → 그대로 사용
  INFERRED 되묻지 않고 자동 추론(유일 후보 / 전체·전국 기본값 / 최신 시점)
  ASK      후보가 여럿이라 판별 불가 → 사용자에게 되물음(재질의)
  NO_DATA  표에 해당 축(후보)이 아예 없음 → 통계없음/불명확

프로젝트 개요의 물음 "무엇을 되묻고 무엇을 추론하나 / 언제 통계없음으로 판단하나"에
대한 결정론적 규칙이 본체다. 네트워크 호출이 없어(슬롯 상태만 받아 판정) 단독으로
테스트·시연할 수 있다. 후보 코드값을 실제 getMeta로 채우는 일은 후속 agent_slots.py,
자연어 질문 파싱은 agent_query_parser.py가 맡는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# candidates 중 이 라벨을 가진 후보가 있으면 되묻지 않고 그 값으로 추론한다
# (KOSIS 분류에서 '전체 집계'에 해당하는 상투적 라벨들).
DEFAULT_LABELS = ("전체", "전국", "계", "합계", "총계", "소계", "전산업", "전국계", "합계(계)", "총합계")

# 되물을 때 한 번에 보여줄 최대 후보 수 — 36개씩 쏟아내지 않는다.
MAX_OPTIONS_SHOWN = 5


class SlotKind(Enum):
    ITEM = "item"            # 측정 항목 → itmId
    DIMENSION = "dimension"  # 분류 축(성별/지역/연령/산업 등) → objL1..objL8
    PERIOD = "period"        # 시점/주기 → prdSe / startPrdDe·endPrdDe


class Decision(Enum):
    FILLED = "filled"
    INFERRED = "inferred"
    ASK = "ask"
    NO_DATA = "no_data"


# 재질의 질문 우선순위(작을수록 먼저 물음): 항목 > 분류 > 시점
_KIND_PRIORITY = {SlotKind.ITEM: 0, SlotKind.DIMENSION: 1, SlotKind.PERIOD: 2}


@dataclass
class Candidate:
    """getMeta가 주는 한 후보값. code는 KOSIS 코드, label은 사람이 읽는 이름."""

    code: str
    label: str


@dataclass
class Slot:
    name: str                              # 표시명("성별", "항목", "시점")
    kind: SlotKind
    param: str                             # KOSIS 파라미터키("itmId", "objL1", "prdSe")
    candidates: list[Candidate] = field(default_factory=list)  # 표에서 가능한 값(비면 축 없음)
    matched: list[Candidate] = field(default_factory=list)     # 질의에서 해석된 값(0/1/N)
    required: bool = True
    latest_period: str | None = None       # PERIOD 슬롯의 최신 시점(추론 시 사용)
    chosen: Candidate | None = None        # 최종 확정값(FILLED/INFERRED에서 채움)
    top_codes: tuple[str, ...] = ()        # 계층 최상위(가장 집계된) 후보 코드 — 되묻기 대신 기본값으로 씀


@dataclass
class ClarifyResult:
    action: str                            # "proceed" | "ask" | "unverifiable"
    questions: list[str] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)   # param -> chosen.code
    reasons: dict[str, str] = field(default_factory=dict)  # slot.name -> "결정: 사유"


def _top_candidates(slot: Slot) -> list[Candidate]:
    """계층 최상위 후보들(없으면 전체 후보)."""
    if not slot.top_codes:
        return list(slot.candidates)
    tops = [c for c in slot.candidates if c.code in slot.top_codes]
    return tops or list(slot.candidates)


# '전체가구', '전체 평균'처럼 집계값인데 라벨이 정확히 '전체'가 아닌 경우가 많다.
# 앞머리 일치로 잡되, '계약직'이 '계'에 걸리는 식의 오탐을 막으려 한 글자 접두는 쓰지 않는다.
AGG_PREFIXES = ("전체", "전국", "합계", "총계", "소계", "전산업", "평균")


def _is_aggregate(label: str) -> bool:
    return label in DEFAULT_LABELS or any(label.startswith(p) for p in AGG_PREFIXES)


def force_choice(slot: Slot) -> Candidate | None:
    """되묻기에 유효한 답을 못 받았을 때 쓸 최선의 값(집계값 → 최상위 → 첫 후보)."""
    c = _default_candidate(slot)
    if c:
        return c
    tops = _top_candidates(slot)
    if tops:
        return tops[0]
    return slot.candidates[0] if slot.candidates else None


def _default_candidate(slot: Slot) -> Candidate | None:
    """되묻지 않고 쓸 기본값을 고른다.

    ① '전체/전국/계…' 집계 라벨이 있으면 그것
    ② 계층 최상위(부모 없는 값)가 딱 하나면 그것 — 예: 자산별의 '고정자산'
       (하위 '건설자산·주거용건물'까지 사용자에게 고르라고 묻지 않기 위함)
    없으면 None.
    """
    for c in slot.candidates:
        if _is_aggregate(c.label):        # '전체', '전체가구', '전체 평균' 등 앞머리 일치 포함
            return c
    tops = [c for c in slot.candidates if c.code in slot.top_codes] if slot.top_codes else []
    if len(tops) == 1:
        return tops[0]
    return None


def decide(slot: Slot) -> Decision:
    """슬롯 하나의 상태로 결정을 낸다.

    2026-07-24 개선 — **분류·항목 값은 사용자에게 되묻지 않는다**(ASK 폐지).
    값 결정은 에이전트 몫이므로, 매칭이 애매하면 되묻는 대신 결정론 기본값으로
    추론(INFERRED)한다. NO_DATA(축 자체 부재)만 통계없음 신호로 남긴다.
    """
    if slot.required and not slot.candidates:
        return Decision.NO_DATA          # 표에 이 축 자체가 없음
    if len(slot.matched) == 1:
        return Decision.FILLED           # 질의에서 하나로 확정
    if len(slot.matched) >= 2:
        return Decision.INFERRED         # (예외적) 복수 → 되묻지 않고 첫 현저값
    # 여기부터 matched == 0 — 어떤 경우든 되묻지 않고 추론한다
    return Decision.INFERRED


def resolve(slot: Slot) -> Decision:
    """decide 결과에 따라 slot.chosen을 채운다. ASK/NO_DATA는 chosen=None."""
    d = decide(slot)
    if d is Decision.FILLED:
        slot.chosen = slot.matched[0]
    elif d is Decision.INFERRED:
        if slot.matched:
            slot.chosen = slot.matched[0]                 # 현저값(복수여도 첫 값)
        elif len(slot.candidates) == 1:
            slot.chosen = slot.candidates[0]
        elif slot.kind is SlotKind.PERIOD:
            # 시점은 코드가 아니라 최신 시점 문자열을 확정값으로 쓴다
            slot.chosen = Candidate(code=slot.latest_period or "", label=slot.latest_period or "최신")
        else:
            # 집계 기본값 없으면 force_choice(집계→최상위→첫 후보)로 반드시 하나 확정 — 되묻지 않는다
            slot.chosen = _default_candidate(slot) or force_choice(slot)
    else:  # ASK, NO_DATA
        slot.chosen = None
    return d


def render_question(slot: Slot) -> str:
    """ASK 슬롯을 되물을 질문으로 만든다.

    후보를 통째로 쏟지 않는다 — 계층 최상위 위주로 최대 MAX_OPTIONS_SHOWN개만 보여주고
    나머지는 "…외 N개"로 줄인다(사용자가 카테고리를 타고 들어가게 만들지 않기 위함).
    """
    opts = slot.matched if len(slot.matched) >= 2 else _top_candidates(slot)
    if not opts:
        return f"'{slot.name}'을(를) 특정할 수 없습니다."
    shown = opts[:MAX_OPTIONS_SHOWN]
    labels = " / ".join(c.label for c in shown)
    rest = len(opts) - len(shown)
    if rest > 0:
        labels += f" …외 {rest}개"
    return f"'{slot.name}'은(는) 어느 기준으로 볼까요? {labels}"


def apply_answer(slot: Slot, answer: str) -> bool:
    """사용자 답변(라벨 또는 코드)을 슬롯에 반영. 매칭 성공 시 True."""
    a = answer.strip()
    for c in slot.candidates:
        if a == c.label or a == c.code:
            slot.matched = [c]
            slot.chosen = c
            return True
    return False


def evaluate(slots: list[Slot]) -> ClarifyResult:
    """전 슬롯을 종합해 다음 행동(proceed/ask/unverifiable)을 결정한다."""
    result = ClarifyResult(action="proceed")
    ask_slots: list[Slot] = []
    unverifiable = False

    for slot in slots:
        d = resolve(slot)
        chosen = slot.chosen.label if slot.chosen else "-"
        result.reasons[slot.name] = f"{d.value} (확정값={chosen})"
        if d is Decision.NO_DATA:
            unverifiable = True
        elif d is Decision.ASK:
            ask_slots.append(slot)
        elif slot.chosen is not None:
            result.filled[slot.param] = slot.chosen.code

    if unverifiable:
        result.action = "unverifiable"
        result.filled = {}
        result.questions = []
        return result

    if ask_slots:
        result.action = "ask"
        result.filled = {}  # 되물을 게 남았으면 아직 호출 파라미터를 확정하지 않는다
        ask_slots.sort(key=lambda s: _KIND_PRIORITY.get(s.kind, 9))
        result.questions = [render_question(s) for s in ask_slots]
        return result

    return result


# ---------------------------------------------------------------------------
# 데모/자체 테스트 — python src/agent_clarify.py
# ---------------------------------------------------------------------------
def _scenario_normal() -> list[Slot]:
    """시나리오1: 정상 자동채움 → proceed."""
    return [
        Slot("항목", SlotKind.ITEM, "itmId",
             candidates=[Candidate("T1", "혼인건수")],           # 유일 후보 → INFERRED
             matched=[]),
        Slot("성별", SlotKind.DIMENSION, "objL1",
             candidates=[Candidate("0", "전체"), Candidate("1", "남자"), Candidate("2", "여자")],
             matched=[]),                                         # 전체 기본값 → INFERRED
        Slot("시점", SlotKind.PERIOD, "prdSe",
             candidates=[], matched=[], required=False, latest_period="2024"),  # 최신 → INFERRED
    ]


def _scenario_ask() -> list[Slot]:
    """시나리오2: 항목이 모호해도 되묻지 않고 자동선택(개선 후) → proceed."""
    return [
        Slot("항목", SlotKind.ITEM, "itmId",
             candidates=[Candidate("T1", "혼인건수"), Candidate("T2", "조혼인율")],
             matched=[]),                                         # 후보2·기본값없음 → 되묻지 않고 첫 후보
        Slot("시점", SlotKind.PERIOD, "prdSe",
             candidates=[], matched=[], required=False, latest_period="2024"),
    ]


def _scenario_no_data() -> list[Slot]:
    """시나리오3: 필수 분류 후보 0 → unverifiable(통계없음)."""
    return [
        Slot("항목", SlotKind.ITEM, "itmId",
             candidates=[Candidate("T1", "휘발유가격")], matched=[]),
        Slot("지역", SlotKind.DIMENSION, "objL1",
             candidates=[], matched=[], required=True),            # 후보0 → NO_DATA
    ]


def _print_result(title: str, res: ClarifyResult) -> None:
    print(f"\n■ {title}")
    print(f"  action = {res.action}")
    for name, reason in res.reasons.items():
        print(f"    - {name}: {reason}")
    if res.questions:
        for q in res.questions:
            print(f"  재질의> {q}")
    if res.filled:
        print(f"  filled(호출 파라미터) = {res.filled}")
