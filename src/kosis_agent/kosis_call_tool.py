"""kosis_call_tool.py — 실전2 API 호출 툴.

재질의(agent_clarify)가 슬롯을 다 채우면(filled: itmId/objL../prdSe), 그 파라미터로
KOSIS `get_data`를 호출해 실제 수치 셀을 가져와 표연산이 바로 쓸 형태로 정규화한다.
또 `getMeta(ITM)`를 항목/분류축으로 구조화해 재질의의 실제 후보 공급원을 제공한다.

기존 kosis_client.get_data/get_meta를 재사용하며(신규 API 코드 없음), 이 모듈은
파라미터 조립·응답 정규화·예외 처리만 담당한다.

  파이프라인 위치:  재질의(filled) → [이 모듈] get_data → 셀 정규화 → table_ops(계산)
  메타 위치:        getMeta(ITM) → [이 모듈] TableMeta → agent_slots → 재질의 슬롯 후보

실행:  .\.venv\Scripts\python.exe src\kosis_call_tool.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

import kosis_client  # 같은 src 디렉터리. get_data/get_meta 재사용

# 한글 주기명 → KOSIS prdSe 코드
PERIOD_CODE = {"년": "Y", "분기": "Q", "월": "M", "일": "D", "반기": "H"}


@dataclass
class Cell:
    """get_data 응답 1행(=시점×분류값×항목의 값 하나)을 정규화한 셀."""

    period: str                       # PRD_DE
    value_raw: str                    # DT 원문
    value_num: float | None           # 수치 변환(비수치면 None)
    unit: str | None                  # UNIT_NM
    item_id: str | None               # ITM_ID
    item_name: str | None             # ITM_NM
    dims: dict[str, str] = field(default_factory=dict)   # {C{n}_OBJ_NM: C{n}_NM}
    last_chg: str | None = None       # LST_CHN_DE


@dataclass
class DimValue:
    """분류축의 값 하나. parent(UP_ITM_ID)가 없으면 최상위(가장 집계된) 값."""

    code: str                         # ITM_ID (objL 파라미터에 넣는 코드)
    label: str                        # ITM_NM
    parent: str | None = None         # UP_ITM_ID (없으면 최상위)


@dataclass
class DimMeta:
    obj_id: str                       # 예 "A"
    obj_nm: str                       # 예 "자산별"
    values: list[DimValue] = field(default_factory=list)

    @property
    def top_level(self) -> list[DimValue]:
        """부모가 없는 값들 = 가장 집계된 상위 항목(되묻기 대신 기본값 후보)."""
        return [v for v in self.values if not v.parent]


@dataclass
class TableMeta:
    org_id: str
    tbl_id: str
    items: list[tuple[str, str, str | None]] = field(default_factory=list)  # (ITM_ID, ITM_NM, UNIT_NM)
    dimensions: list[DimMeta] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list)


def parse_table_key(table_key: str) -> tuple[str, str]:
    """'101:DT_x' → ('101', 'DT_x')."""
    if ":" not in table_key:
        raise ValueError(f"table_key는 'org_id:tbl_id' 형식이어야 합니다: {table_key!r}")
    org_id, tbl_id = table_key.split(":", 1)
    if not org_id or not tbl_id:
        raise ValueError(f"table_key 파싱 실패: {table_key!r}")
    return org_id, tbl_id


def _to_num(raw: str | None) -> float | None:
    """DT 문자열을 float으로. 비수치('-','…',공백 등)면 None."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def period_code(period_type: str | None) -> str:
    """'년'/'월'/'분기' 등 한글 주기명을 prdSe 코드로. 이미 코드면 그대로."""
    if not period_type:
        return "Y"
    p = str(period_type).strip()
    if p in PERIOD_CODE.values():        # 이미 Y/Q/M/D/H
        return p
    return PERIOD_CODE.get(p, "Y")


def fetch_meta(table_key: str) -> TableMeta:
    """getMeta(ITM)를 항목(OBJ_ID==ITEM)과 분류축(그 외)으로 구조화한다."""
    org_id, tbl_id = parse_table_key(table_key)
    rows = kosis_client.get_meta(org_id, tbl_id, "ITM")
    if not isinstance(rows, list):
        raise RuntimeError(f"getMeta 응답이 리스트가 아닙니다: {type(rows)}")

    meta = TableMeta(org_id=org_id, tbl_id=tbl_id, raw=rows)
    dim_map: dict[str, DimMeta] = {}     # obj_id → DimMeta (등장 순서 보존)
    for r in rows:
        obj_id = str(r.get("OBJ_ID") or "").strip()
        if obj_id == "ITEM":
            meta.items.append((r.get("ITM_ID"), r.get("ITM_NM"), r.get("UNIT_NM")))
        else:
            dm = dim_map.get(obj_id)
            if dm is None:
                dm = DimMeta(obj_id=obj_id, obj_nm=str(r.get("OBJ_NM") or "").strip())
                dim_map[obj_id] = dm
                meta.dimensions.append(dm)
            up = r.get("UP_ITM_ID")
            dm.values.append(DimValue(
                code=str(r.get("ITM_ID") or ""),
                label=str(r.get("ITM_NM") or ""),
                parent=str(up) if up else None,
            ))
    return meta


def _row_to_cell(r: dict) -> Cell:
    """get_data 응답 1행 → Cell. C1..C8 분류값을 dims로 모은다."""
    dims: dict[str, str] = {}
    for n in range(1, 9):
        obj_nm = r.get(f"C{n}_OBJ_NM")
        val_nm = r.get(f"C{n}_NM")
        if obj_nm and val_nm:
            dims[str(obj_nm)] = str(val_nm)
    return Cell(
        period=str(r.get("PRD_DE") or ""),
        value_raw=str(r.get("DT") if r.get("DT") is not None else ""),
        value_num=_to_num(r.get("DT")),
        unit=r.get("UNIT_NM"),
        item_id=r.get("ITM_ID"),
        item_name=r.get("ITM_NM"),
        dims=dims,
        last_chg=r.get("LST_CHN_DE"),
    )


def fetch_cells(table_key: str, itm_id: str, obj_levels: dict[str, str],
                prd_se: str = "Y", start: str | None = None,
                end: str | None = None, new_est_prd_cnt: int | None = None) -> list[Cell]:
    """get_data를 호출해 정규화된 셀 리스트를 반환한다.

    obj_levels 예: {"objL1": "A10101"} — 분류축이 여러 개면 objL2.. 추가.
    new_est_prd_cnt를 주면 start/end 대신 '최근 N개 시점'을 조회한다(분기·상대시점 처리에 사용).
    빈 결과면 [] (표연산에서 UNVERIFIABLE로 연결 가능).
    """
    org_id, tbl_id = parse_table_key(table_key)
    obj_l1 = obj_levels.get("objL1", "")
    extra = {k: v for k, v in obj_levels.items() if k != "objL1"}   # objL2.. → get_data(**extra)
    if new_est_prd_cnt:                       # 최근 N시점 조회 → 기간 경계 포맷 문제 회피
        extra["newEstPrdCnt"] = str(new_est_prd_cnt)
        start = end = None
    try:
        data = kosis_client.get_data(
            org_id, tbl_id, obj_l1=obj_l1, itm_id=itm_id,
            prd_se=prd_se, start_prd_de=start, end_prd_de=end, **extra,
        )
    except RuntimeError as e:
        print(f"[경고] get_data 오류 → 빈 결과 처리: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print(f"[경고] get_data 응답이 리스트가 아님: {type(data)}", file=sys.stderr)
        return []
    return [_row_to_cell(r) for r in data]


def cells_from_filled(table_key: str, filled: dict[str, str],
                      start: str | None = None, end: str | None = None,
                      prd_se: str | None = None,
                      new_est_prd_cnt: int | None = None) -> list[Cell]:
    """재질의 결과 filled({itmId, objL1.., prdSe})를 바로 fetch_cells로 변환하는 편의 함수.

    prd_se를 넘기면 filled의 prdSe보다 우선(파서가 정한 주기 Y/Q/M 반영).
    new_est_prd_cnt를 넘기면 최근 N시점 조회(분기·상대시점).
    """
    itm_id = filled.get("itmId") or ""
    if not itm_id:
        raise ValueError("filled에 itmId가 없습니다(재질의 미완료).")
    obj_levels = {k: v for k, v in filled.items() if k.startswith("objL")}
    code = prd_se or filled.get("prdSe") or "Y"
    if code not in PERIOD_CODE.values():       # 혹시 한글 주기명이면 코드로
        code = period_code(code)
    return fetch_cells(table_key, itm_id, obj_levels, code, start, end, new_est_prd_cnt)
