"""table_ops.py — 실전2 표 연산(종합의 검증 계산 엔진).

API 호출(kosis_call_tool.fetch_cells)이 돌려준 정규화 셀(Cell) 리스트를 받아
결정론으로 계산한다: 특정/최신 시점 값, 증감·증감률(전년대비), 합계, 구성비/비율.
모든 결과는 근거 셀(basis)과 계산식(formula)을 달고 나와, 종합의 tolerance_judge가
그대로 비교에 쓸 수 있다.

핵심 원칙(작업계획서 §2):
  · 정렬 실패를 조용히 넘기지 않는다 — 요청 시점/분류가 없거나 애매하면 실패로 표시(note),
    임의로 가까운 셀을 끌어오지 않는다.
  · 결정론만 — value_num 사칙연산. 비수치 셀은 계산에서 제외+사유 기록.
  · 근사 판정은 여기서 하지 않는다 — tolerance_judge.py 몫. 이 모듈은 실측값만 낸다.
  · 단위 혼재는 계산하되 경고(단위 변환은 하지 않음).

실행:  .\.venv\Scripts\python.exe src\table_ops.py
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kosis_call_tool import Cell


@dataclass
class OpResult:
    op: str
    value: float | None
    unit: str | None
    formula: str
    basis: list[tuple] = field(default_factory=list)   # [(period, dims, value_num)]
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


def _basis(cells: list[Cell]) -> list[tuple]:
    return [(c.period, dict(c.dims), c.value_num) for c in cells]


def _numeric(cells: list[Cell]) -> tuple[list[Cell], str]:
    """value_num이 있는 셀만 남기고, 제외 건수를 note 문자열로 반환한다."""
    good = [c for c in cells if c.value_num is not None]
    dropped = len(cells) - len(good)
    note = f"비수치 셀 {dropped}건 제외" if dropped else ""
    return good, note


def _one_unit(cells: list[Cell]) -> tuple[str | None, str]:
    """단위가 유일하면 그 단위, 섞이면 (None, 경고)."""
    units = {c.unit for c in cells if c.unit}
    if len(units) == 1:
        return next(iter(units)), ""
    if len(units) > 1:
        return None, f"단위 혼재 경고: {sorted(units)}"
    return None, ""


def _round(x: float | None) -> float | None:
    return round(x, 2) if x is not None else None


def value_at(cells: list[Cell], period: str) -> OpResult:
    """해당 시점 셀 1개의 값. 0개면 실패, 2개 이상(분류 미지정)이면 실패(임의 선택 금지)."""
    good, drop_note = _numeric(cells)
    hit = [c for c in good if c.period == period]
    if not hit:
        return OpResult("value_at", None, None, f"period={period} 셀 없음",
                        _basis(cells), "; ".join(filter(None, ["요청 시점 부재", drop_note])))
    if len(hit) > 1:
        return OpResult("value_at", None, None, f"period={period} 셀 {len(hit)}개",
                        _basis(hit), f"분류가 더 좁혀져야 함(셀 {len(hit)}개) — 임의 선택 안 함")
    c = hit[0]
    return OpResult("value_at", c.value_num, c.unit,
                    f"{c.value_num} @ {period} {c.dims or ''}".strip(),
                    _basis(hit), drop_note)


def latest(cells: list[Cell]) -> OpResult:
    """가장 최근(최대 period) 값."""
    good, drop_note = _numeric(cells)
    if not good:
        return OpResult("latest", None, None, "수치 셀 없음", _basis(cells), drop_note)
    period = max(c.period for c in good)
    r = value_at(cells, period)
    r.op = "latest"
    return r


def change(cells: list[Cell], base_period: str, target_period: str) -> OpResult:
    """증감률(%). value=pct, note에 증감액(delta)."""
    b = value_at(cells, base_period)
    t = value_at(cells, target_period)
    if not (b.ok and t.ok):
        why = "; ".join(filter(None, [f"기준({base_period}): {b.note}" if not b.ok else "",
                                      f"대상({target_period}): {t.note}" if not t.ok else ""]))
        return OpResult("change", None, b.unit or t.unit, "증감 계산 불가", b.basis + t.basis, why)
    if b.value == 0:
        return OpResult("change", None, b.unit, "분모(기준값) 0", b.basis + t.basis, "0으로 나눔")
    delta = t.value - b.value
    pct = delta / b.value * 100
    unit, unit_note = _one_unit([c for c in cells if c.period in (base_period, target_period)])
    return OpResult(
        "change", _round(pct), "%",
        f"({t.value}-{b.value})/{b.value}*100 = {_round(pct)}%  (증감액 {_round(delta)}{unit or ''})",
        b.basis + t.basis, unit_note)


def yoy(cells: list[Cell]) -> list[OpResult]:
    """전년대비 시계열 — 인접한 두 시점의 change 리스트."""
    good, _ = _numeric(cells)
    periods = sorted({c.period for c in good})
    return [change(cells, periods[i - 1], periods[i]) for i in range(1, len(periods))]


def total(cells: list[Cell], period: str | None = None) -> OpResult:
    """합계 — (period 지정 시 그 시점의) 셀 값 합."""
    good, drop_note = _numeric(cells)
    if period is not None:
        good = [c for c in good if c.period == period]
    if not good:
        return OpResult("total", None, None, "합산할 셀 없음", _basis(cells), drop_note)
    unit, unit_note = _one_unit(good)
    s = sum(c.value_num for c in good)
    terms = "+".join(str(c.value_num) for c in good)
    return OpResult("total", _round(s), unit, f"{terms} = {_round(s)}",
                    _basis(good), "; ".join(filter(None, [drop_note, unit_note])))


def share(part_value: float, whole_value: float, unit: str | None = None) -> OpResult:
    """구성비(%) = part/whole*100."""
    if whole_value == 0:
        return OpResult("share", None, "%", "분모(전체) 0", [], "0으로 나눔")
    pct = part_value / whole_value * 100
    return OpResult("share", _round(pct), "%",
                    f"{part_value}/{whole_value}*100 = {_round(pct)}%", [], "")


def ratio(a: float, b: float) -> OpResult:
    """비율/배수 = a/b."""
    if b == 0:
        return OpResult("ratio", None, "배", "분모 0", [], "0으로 나눔")
    return OpResult("ratio", _round(a / b), "배", f"{a}/{b} = {_round(a / b)}배", [], "")
