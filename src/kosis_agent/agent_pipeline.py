"""agent_pipeline.py — 실전2 대화 오케스트레이터(결정론).

한 문장을 받아 파싱→(표 매핑)→슬롯화→정합성 게이트→API 호출→표연산→최종 답변까지
순서대로 굴린다.

2026-07-24 폴백 추가 — 자동 매핑 모드에서 매핑이 고른 표가 부적합/무데이터면 되묻거나
바로 포기하지 않고 **후보(리랭커 Top-5) 다음 표로 자동 재시도**한다. 첫 성공 채택.
table_key를 명시로 주면 단일 표(폴백 없음, 기존 동작 보존).

실행:  .\.venv\Scripts\python.exe src\agent_pipeline.py
"""
from __future__ import annotations

import re

from agent_clarify import evaluate, force_choice, SlotKind
from agent_query_parser import parse
from agent_slots import build_slots, unresolved_qualifiers, unresolved_indicator
from kosis_call_tool import cells_from_filled
from table_ops import change, value_at, total, ratio, OpResult
from tolerance_judge import judge, parse_large_number

# 문장 속 주장값 토큰: "65만8천원", "8만8천원", "2.2%", "20만건" 등
_CLAIM_VAL_RE = re.compile(r"([0-9][0-9.,조억만천백]*)\s*(원|명|가구|건|개|%|배|톤)")
_VERDICT_KO = {"MATCH": "일치", "MOSTLY_MATCH": "대체로 일치",
               "MISMATCH": "불일치", "NEEDS_REVIEW": "재검토"}


def _auto_verdict(question: str, cells: list, target_period: str | None,
                  change_result: OpResult):
    """문장의 주장값을 뽑아 실측값과 tolerance_judge로 대조 → 일치/불일치.

    - 금액·인원 등 '레벨' 주장(원/명/건…)은 대상 시점의 실측 레벨값과 비교(보도된 '수치').
    - '%' 주장은 실측 증감률과 비교. 주장값이 없으면 None(판정 생략).
    """
    tokens = [(f"{num}{unit}", unit) for num, unit in _CLAIM_VAL_RE.findall(question or "")]
    if not tokens:
        return None
    level = value_at(cells, target_period) if target_period else None
    # ① 레벨 주장(원/명/건 등, 실측 레벨과 단위 일치) — 보도 수치는 유효숫자 반올림이라
    #    tolerance_judge의 EXACT(절대 0.05pp, %전용) 대신 **반올림 상대오차**로 판정한다.
    #    (보도 수치는 대개 문장 뒤쪽이라 마지막 토큰 채택)
    if level and level.ok and level.unit:
        same = [frag for frag, unit in tokens if unit == level.unit]
        claimed = parse_large_number(same[-1]) if same else None
        if claimed is not None and level.value:
            rel = abs(claimed - level.value) / abs(level.value) * 100
            v = "MATCH" if rel <= 0.5 else ("MOSTLY_MATCH" if rel <= 2 else "MISMATCH")
            return v, same[-1], level.value, level.unit, round(rel, 3)
    # ② % 주장 — 실측 증감률과 tolerance_judge로 비교(등식형 규칙)
    pct = [frag for frag, unit in tokens if unit == "%"]
    if pct and change_result and change_result.ok:
        jr = judge(pct[0], change_result.value, official_unit="%")
        return jr.verdict.value, pct[0], change_result.value, "%", jr.metric
    return None


def _chosen_labels(slots) -> dict[str, str]:
    """param → 확정 라벨 (답변 문장용)."""
    return {s.param: (s.chosen.label if s.chosen else "") for s in slots}


def _phrase(parsed, r: OpResult, labels: dict[str, str]) -> str:
    """표연산 결과 → 자연어 답변."""
    item = labels.get("itmId", "") or "해당 지표"
    # 분류축 값(분위·가계수지항목=흑자액 등)을 모두 앞에 붙여 무엇의 값인지 분명히 한다.
    dims = " ".join(labels[p] for p in sorted(labels) if p.startswith("objL") and labels[p])
    subject = f"{dims} {item}".strip()
    if not r.ok:
        return f"{subject}: 계산할 수 없습니다({r.note})."
    if parsed.op == "change":
        d = "증가" if r.value > 0 else ("감소" if r.value < 0 else "변화 없음")
        return f"{subject}은(는) {parsed.target_year}년 전년比 {abs(r.value)}% {d}했습니다."
    if parsed.op == "value_at":
        return f"{subject}의 {parsed.target_year}년 값은 {r.value}{r.unit or ''}입니다."
    return f"{subject}: {r.op} = {r.value}{r.unit or ''}"


def _period_ko(parsed) -> str:
    """읽기용 시점 문자열: '2024년 4분기' / '2024년 12월' / '2024년'."""
    ty = parsed.target_year
    tp = parsed.target_period or ""
    if parsed.prd_se == "Q" and len(tp) == 6:
        return f"{ty}년 {int(tp[-2:])}분기"
    if parsed.prd_se == "M" and len(tp) == 6:
        return f"{ty}년 {int(tp[-2:])}월"
    return f"{ty}년"


def _kosis_path(table_key: str, tbl_name: str, labels: dict[str, str], parsed) -> str:
    """값이 KOSIS 표 어디에 있는지 사용자용 경로 한 줄로. (결정론 — LLM 안 거침)

    예) 통계청 › 소득5분위별 가구당 가계수지(전국,1인이상,실질) › 전체가구 › 3분위 › 흑자액 › 2024년 4분기  [101:DT_1L9U122]
    """
    org = (table_key or "").split(":")[0]
    org_ko = "통계청" if org == "101" else (org or "KOSIS")
    steps: list[str] = []
    if labels.get("itmId"):
        steps.append(labels["itmId"])                     # 항목(전체가구 등)
    for p in sorted(labels):                              # 분류축(분위·흑자액 등)
        if p.startswith("objL") and labels[p]:
            steps.append(labels[p])
    steps.append(_period_ko(parsed))                     # 시점
    trail = " › ".join([org_ko, tbl_name or table_key] + steps)
    return f"{trail}  [{table_key}]"


def _build_facts(question, slots, parsed, cells, tbl_name, vd):
    """결정론이 확정한 값만으로 설명 생성용 facts를 만든다(수치는 여기서 고정)."""
    tp, bp = parsed.target_period, parsed.base_period
    lv = value_at(cells, tp) if tp else None
    bv = value_at(cells, bp) if bp else None
    unit = (lv.unit if lv and lv.ok else None) or ""
    dims = " · ".join(f"{s.name}={s.chosen.label}" for s in slots if s.chosen)
    facts = {
        "기관": "통계청",
        "통계표": tbl_name or "",
        "항목·분류": dims,
        "시점": _period_ko(parsed),
    }
    if lv and lv.ok:
        facts["실측값"] = f"{lv.value:,.0f}{unit}"
    if bv and bv.ok:
        facts["비교시점값"] = f"전년동기({parsed.base_year}년) {bv.value:,.0f}{unit}"
        if lv and lv.ok:
            facts["증감"] = f"{lv.value - bv.value:+,.0f}{unit}"
    if vd:
        verdict, claimed_frag, official_val, vunit, metric = vd
        facts["기사표현"] = claimed_frag
        if metric is not None:
            facts["오차"] = f"{metric}%" if vunit != "%" else f"{metric}"
        facts["허용기준"] = "0.5% 이내" if vunit != "%" else "표현유형별 허용오차"
        facts["판정"] = _VERDICT_KO.get(verdict, verdict)
    return facts


def _attempt_table(question: str, table_key: str, parsed, verbose: bool = True,
                   explain: bool = False, tbl_name: str = "") -> dict:
    """표 1개로 검증을 시도한다. status ∈ {answer, infeasible, axis_missing, no_data}.

    indicator_matched: 주장의 지표·분류가 실제로 질의에서 잡혔는지(품질 신호).
    explain=True면 DASH-002로 자연어 근거 설명을 생성해 답변으로 쓴다(숫자·판정은 불변).
    되묻지 않는다 — 실패면 상태만 돌려주고 상위(run)가 다음 표로 폴백한다.
    """
    slots, _meta = build_slots(table_key, question)
    indicator_matched = any(
        s.matched for s in slots if s.kind in (SlotKind.ITEM, SlotKind.DIMENSION))

    # 정합성 게이트 — 주장 한정어('3분위' 등)가 표에 없으면 이 표로는 검증 불가
    infeasible = unresolved_qualifiers(question, slots)
    if infeasible:
        return {"status": "infeasible", "table_key": table_key,
                "infeasible": infeasible, "indicator_matched": indicator_matched}

    # 지표 게이트 — 주장 지표(흑자액/생산자본스톡 등)가 이 표의 항목/분류값/표명에 없으면 폴백
    absent = unresolved_indicator(question, slots, table_key)
    if absent:
        return {"status": "indicator_absent", "table_key": table_key,
                "indicator": absent, "indicator_matched": indicator_matched}

    res = evaluate(slots)
    if res.action == "unverifiable":       # 필수 축 자체가 표에 없음(NO_DATA)
        return {"status": "axis_missing", "table_key": table_key,
                "indicator_matched": indicator_matched}

    filled = res.filled
    labels = _chosen_labels(slots)
    # 주기(연/분기/월) + 상대시점(작년/전년) → 최근 N시점 조회 후 target/base_period로 계산.
    # 기간 경계 포맷(YYYYQ vs YYYYMM) 문제를 피하려 newEstPrdCnt로 받아 PRD_DE로 직접 매칭.
    prd_se = parsed.prd_se or "Y"
    cnt = {"Y": 15, "Q": 24, "M": 48, "H": 12, "D": 60}.get(prd_se, 15)

    cells = cells_from_filled(table_key, filled, prd_se=prd_se, new_est_prd_cnt=cnt)
    if not cells:                          # 완화 재시도 1회 — 분류축을 집계 기본값으로
        relaxed = dict(filled)
        changed = False
        for s in slots:
            if s.kind is SlotKind.DIMENSION:
                dc = force_choice(s)
                if dc and relaxed.get(s.param) != dc.code:
                    relaxed[s.param] = dc.code
                    changed = True
        if changed:
            if verbose:
                print(f"    [완화 재시도] 분류축→집계 기본값 재조회: {relaxed}")
            cells = cells_from_filled(table_key, relaxed, prd_se=prd_se, new_est_prd_cnt=cnt)
            if cells:
                filled = relaxed
    if not cells:
        return {"status": "no_data", "table_key": table_key,
                "filled": filled, "indicator_matched": indicator_matched}

    tp, bp = parsed.target_period, parsed.base_period
    if parsed.op == "change" and bp and tp:
        r = change(cells, bp, tp)
    elif parsed.op == "total" and tp:
        r = total(cells, period=tp)
    elif tp:
        r = value_at(cells, tp)
    else:
        r = value_at(cells, cells[-1].period)

    answer = _phrase(parsed, r, labels)
    auto = [f"{s.name} {s.chosen.label}" for s in slots
            if s.kind is SlotKind.DIMENSION and s.chosen and not s.matched and len(s.candidates) > 1]
    if auto:
        answer += f"  ※ {', '.join(auto)} 기준입니다. 다른 기준이 필요하면 말씀해 주세요."

    # 종합 판정 — 문장의 주장값을 실측값과 대조(tolerance_judge)해 일치/불일치를 붙인다.
    verdict = None
    verdict_line = ""
    vd = _auto_verdict(question, cells, tp, r)
    if vd:
        verdict, claimed_frag, official_val, unit, _metric = vd
        ko = _VERDICT_KO.get(verdict, verdict)
        verdict_line = (f"판정: {ko} (뉴스 주장 {claimed_frag} vs 실측 "
                        f"{round(official_val, 2)}{unit or ''})")
        answer += f"\n  → {verdict_line}"

    # 설명 생성(선택) — DASH-002가 결정론 사실을 자연어 근거로 다듬는다. 숫자·판정 불변.
    explanation = None
    if explain:
        from agent_explain import explain as _gen_explain
        facts = _build_facts(question, slots, parsed, cells, tbl_name, vd)
        explanation = _gen_explain(facts)
        if explanation:
            answer = explanation + (f"\n\n— {verdict_line}" if verdict_line else "")

    # KOSIS 경로 한 줄(결정론) — 값이 표 어디서 왔는지 사용자에게 항상 보여준다.
    path_line = "📍 경로: " + _kosis_path(table_key, tbl_name, labels, parsed)
    answer = f"{answer}\n\n{path_line}"

    return {"status": "answer", "action": "answer", "answer": answer, "table_key": table_key,
            "filled": filled, "op_result": r, "labels": labels,
            "indicator_matched": indicator_matched, "verdict": verdict,
            "explanation": explanation}


def _order_candidates(sel: dict) -> list[str]:
    """[RAG가 고른 표] + [나머지 후보 랭킹순], 중복 제거."""
    chosen = sel.get("table_key")
    cands = sel.get("candidates") or []
    ranked = [c["table_key"] for c in sorted(cands, key=lambda x: x.get("final_rank", 99))]
    ordered, seen = [], set()
    for tk in ([chosen] + ranked):
        if tk and tk not in seen:
            ordered.append(tk)
            seen.add(tk)
    return ordered


def _finalize(r: dict, parsed, verbose: bool, pick_reason: str = "", rank: int | None = None) -> dict:
    """_attempt_table 결과를 최종 반환 형태로 정리·출력."""
    if r["status"] == "answer":
        if verbose:
            weak = "" if r.get("indicator_matched") else "  ⚠약한 채택(지표 미매칭 — 표가 주장과 다를 수 있음)"
            where = f"(후보 {rank}순째 표)" if rank else ""
            print(f"  [filled] {r['filled']}")
            print(f"  [표연산] {r['op_result'].formula}")
            print(f"에이전트: {r['answer']} {where}{weak}")
            if pick_reason:
                print(f"  └ 표 선택 근거: {pick_reason}")
            print()
        return {"answer": r["answer"], "action": "answer", "parsed": parsed,
                "table_key": r["table_key"], "filled": r["filled"], "op_result": r["op_result"],
                "indicator_matched": r.get("indicator_matched"), "pick_reason": pick_reason,
                "verdict": r.get("verdict"), "explanation": r.get("explanation"),
                "fallback_tried": r.get("fallback_tried")}
    # 단일 표 실패(명시 지정 경로)
    msg = {"infeasible": f"이 표에는 '{', '.join(r.get('infeasible', []))}' 구분이 없어 검증할 수 없습니다(다른 통계표가 필요).",
           "indicator_absent": f"이 표에는 '{r.get('indicator', '')}' 항목이 없어 검증할 수 없습니다(다른 통계표가 필요).",
           "axis_missing": "이 표에는 필요한 분류 축이 없어 검증할 수 없습니다.",
           "no_data": "해당 조건에 맞는 수치를 KOSIS에서 찾지 못했습니다(통계없음/불명확)."}.get(
               r["status"], "검증할 수 없습니다.")
    if verbose:
        print(f"에이전트: {msg}\n")
    return {"answer": msg, "action": "unverifiable", "parsed": parsed,
            "table_key": r.get("table_key"), "status": r["status"]}


def run(question: str, table_key: str | None = None, scripted_answers=(),
        verbose: bool = True, answer_fn=None, candidates: list[str] | None = None,
        explain: bool = False) -> dict:
    parsed = parse(question)
    if verbose:
        print(f"사용자: {question}")
        print(f"  [파싱] op={parsed.op} base={parsed.base_year} target={parsed.target_year}")

    # 명시 지정 → 단일 표(폴백 없음, 기존 동작 보존)
    if table_key is not None:
        return _finalize(_attempt_table(question, table_key, parsed, verbose, explain=explain),
                         parsed, verbose)

    # 후보 표들 확보: 주입(candidates)이 있으면 매핑 생략(테스트용), 없으면 자동 매핑
    pick_reason = ""
    name_map: dict[str, str] = {}
    if candidates is not None:
        ordered = list(dict.fromkeys([c for c in candidates if c]))
    else:
        from agent_map import select_table
        sel = select_table(question, verbose=verbose)
        if not sel:
            ans = "질문 주제에 맞는 통계표를 찾지 못했습니다(관련 통계 없음)."
            if verbose:
                print(f"에이전트: {ans}")
            return {"answer": ans, "action": "unverifiable", "parsed": parsed}
        ordered = _order_candidates(sel)
        pick_reason = sel.get("reason") or ""
        name_map = {c["table_key"]: c.get("tbl_name") or "" for c in (sel.get("candidates") or [])}

    tried: list[dict] = []
    for i, tk in enumerate(ordered, 1):
        if verbose:
            print(f"  [후보 시도 {i}/{len(ordered)}] {tk}")
        r = _attempt_table(question, tk, parsed, verbose, explain=explain, tbl_name=name_map.get(tk, ""))
        tried.append({"table_key": tk, "status": r["status"],
                      "indicator_matched": r.get("indicator_matched")})
        if verbose:
            print(f"     → {r['status']} (지표매칭={r.get('indicator_matched')})")
        if r["status"] == "answer":
            r["fallback_tried"] = tried
            return _finalize(r, parsed, verbose, pick_reason=pick_reason, rank=i)

    summ = "; ".join(f"{t['table_key']}:{t['status']}" for t in tried)
    ans = f"후보 {len(ordered)}개를 모두 시도했으나 적합한 표가 없습니다(검증 불가)."
    if verbose:
        print(f"에이전트: {ans}\n    [{summ}]")
    return {"answer": ans, "action": "unverifiable", "parsed": parsed, "fallback_tried": tried}
