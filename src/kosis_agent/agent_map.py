"""agent_map.py — 표 매핑 다리: 질문 → 하이브리드 검색 → 표 후보(table_key).

지금까지 파이프라인은 table_key를 손으로 지정했다. 이 모듈이 그 자리를 메운다:
자연어 질문을 팀원(nayeon)의 하이브리드 검색기(search_hybrid_v2.py)에 넘겨
색인된 600개 표에서 후보 Top-N을 받아온다.

하이브리드 검색기는 자체 CLI라, 여기선 **서브프로세스로 호출**해 stdout의 JSON을 파싱한다
(검색 로직은 D 코드를 그대로 재사용 — 복제/수정하지 않음). 매 호출마다 BM25 색인을
다시 빌드하므로 대량 배치엔 부적합하지만, 대화형 단건 질문엔 충분하다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from agent_slots import build_slots, unresolved_indicator, unresolved_qualifiers

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEARCH_SCRIPT = PROJECT_ROOT / "archive" / "260722" / "src" / "v2" / "search_hybrid_v2.py"
_RERANK = True   # 라이브 표선택에 NCP 리랭커 사용(체인: getMeta→리랭커→RAG). 비교용 토글.
# 검색 recall 튜닝 — 라이브 폴백에서 정답표를 놓치지 않게 후보·재점수 폭을 넓힌다.
# (재점수 개수↑ = getMeta 호출↑ = 조금 느려짐. 로컬은 8 유지, 라이브만 확대.)
# 가져온 후보를 하나도 버리지 않게 검색 수 = 재점수 수로 맞춘다(silent drop 방지).
LIVE_SEARCH_K = 20     # KOSIS 라이브 검색이 가져올 후보 수 (기존 10)
LIVE_RESCORE_N = 20    # 라이브 후보 재점수 개수 = 검색 수와 동일 (기존 8)
# 주어(지표) 명사: '<명사>(부가)?은/는/이/가' — 라이브 검색 질의에 지표를 앞세우기 위함.
_SUBJECT_RE = re.compile(r"([가-힣]{2,})(?:\([^)]*\))?(?:은|는|이|가)(?=[\s,.)]|$)")


def map_question(question: str, top_n: int = 5) -> list[dict]:
    """질문 → 표 후보 리스트 [{final_rank, table_key, tbl_name, fusion_score}, ...].

    검색 실패/후보 없음이면 빈 리스트. 하이브리드 색인(output/kosis_qdrant_v2)이 있어야 한다.
    """
    if not SEARCH_SCRIPT.exists():
        raise FileNotFoundError(f"하이브리드 검색기 없음: {SEARCH_SCRIPT}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(SEARCH_SCRIPT), "--claim-text", question, "--top-n", str(top_n)],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"하이브리드 검색 실패: {(proc.stderr or '')[-500:]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"검색 결과 JSON 파싱 실패: {e}\nstdout 앞부분: {proc.stdout[:300]}")
    return data.get("selected_tables", [])


def search_tables_live(question: str, exclude: set | None = None, k: int = 10,
                       verbose: bool = False) -> list[dict]:
    """KOSIS 라이브 검색(색인 밖 표까지). 로컬 후보에 적합 표가 없을 때만 부른다.

    질의는 표 검색용 키워드로 변환(build_retrieval_query 재사용). 결과를 로컬 후보와
    동일 스키마({table_key, tbl_name, final_rank})로 정규화한다.
    """
    import kosis_client
    exclude = exclude or set()
    # 사용자는 기사 문장만 붙여넣고, KOSIS 질의는 여기서 자동 생성한다.
    # 뉴스 상투어(통계청 국가통계포털 KOSIS에 따르면 / 24일 / 집계됐다)가 문장 앞에 오므로
    # ① 그 상투어·단위·상대시점을 걷어내고 ② 주장의 '주어(지표, 예: 흑자액)'를 질의 앞에 세운 뒤
    # ③ KOSIS는 핵심어 3~4개일 때 잘 맞으니 결과가 없으면 어휘를 줄여가며 재시도한다.
    _SEARCH_NOISE = {"국가", "통계", "포털", "KOSIS", "통계청", "국가통계포털", "데이터",
                     "따르", "발표", "밝힘", "집계", "조사", "기준", "관련", "대비", "가운데", "각각",
                     "상위", "하위", "실질", "명목", "분기", "반기", "월별", "연간",
                     "전년", "작년", "올해", "지난해", "최근", "당해", "이상", "미만",
                     "천원", "만원", "억원", "조원"}
    try:
        from keyword_extractor import extract_keywords
        nouns = [w for w in extract_keywords(question)
                 if w not in _SEARCH_NOISE and not w.isdigit()]
    except Exception:
        nouns = []
    subj = None                             # 주어(지표) — 서술의 '…은/는' 앞 명사
    m = _SUBJECT_RE.search(question or "")
    if m and m.group(1) not in _SEARCH_NOISE:
        subj = m.group(1)
    kws = list(dict.fromkeys(([subj] if subj else []) + nouns))   # 지표를 맨 앞, 중복 제거
    if not kws:
        kws = [question]
    rows = []
    for n in (5, 4, 3, 2):                  # 어휘를 줄여가며(지표 우선 보존) 결과가 나올 때까지 재시도
        n = min(n, len(kws))
        query = " ".join(kws[:n])
        try:
            rows = kosis_client.search_tables(query, result_count=k)
            if rows:
                if verbose:
                    print(f"  [라이브 검색] 질의='{query}' → {len(rows)}건")
                break
        except Exception:                   # err30(결과 없음) 등 → 더 짧은 질의로 재시도
            rows = []
    if not rows:
        if verbose:
            print("  [라이브 검색] 결과 없음")
        return []
    out, rank = [], 0
    for r in rows or []:
        org, tbl = str(r.get("ORG_ID") or ""), str(r.get("TBL_ID") or "")
        if not org or not tbl:
            continue
        tk = f"{org}:{tbl}"
        if tk in exclude:
            continue
        rank += 1
        out.append({"table_key": tk, "tbl_name": r.get("TBL_NM") or tk, "final_rank": rank})
    return out


# ── getMeta 재점수 (원래 agent_rescore.py, 2026-07-28 병합) ──────────────────
# 색인은 표 단위 텍스트만 담아 '흑자액' 같은 하위 값을 못 잡는다. 후보 표마다 getMeta로
# 항목·분류값을 펼쳐 지표·한정어 실재를 보고 feasible/score를 매긴다(후보당 API 1회).
def _context_bonus(question: str, tbl_name: str) -> float:
    """표명 맥락(실질/명목·전국/도시)이 주장과 맞으면 가점, 어긋나면 감점.

    같은 지표라도 표가 명목/실질·전국/도시·1/2인이상으로 갈리므로, 주장의 명시 조건과
    맞는 표를 골라야 값이 뉴스와 일치한다. (예: 뉴스 '실질' → '…실질' 표 선택)
    """
    q, n = question or "", tbl_name or ""
    bonus = 0.0
    if "실질" in q:
        bonus += 2 if "실질" in n else (-2 if "명목" in n else 0)
    elif "명목" in q:
        bonus += 2 if "명목" in n else (-2 if "실질" in n else 0)
    if "도시" in q:
        bonus += 1 if "도시" in n else 0
    else:
        bonus += 0.5 if "전국" in n else (-0.5 if "도시" in n else 0)
    if "2인" in q:
        bonus += 0.5 if "2인" in n else 0
    elif "1인" not in q and "1인이상" in n:
        bonus += 0.3
    return bonus


def _default_score(matched: int, indicator_absent, unresolved: list,
                   question: str, tbl_name: str) -> float:
    """기본 재점수 산식(임시 휴리스틱). 매칭수·지표실재·한정어해소 + 맥락가점의 가중합.

    ※ 임의 가중치(×2, +2)라 검증 대상. 골드셋 22건 표선택 정답률 54.5%(2026-07-28).
    """
    return (matched * 2 + (0 if indicator_absent else 2) + (0 if unresolved else 2)
            + _context_bonus(question, tbl_name))


# ── 점수 산식 교체 지점(pluggable) ─────────────────────────────────────────
# 다른 지표로 갈아끼우려면 이 이름에 다른 함수를 대입한다(코드 수정 없이):
#   import agent_map; agent_map.SCORE_FN = my_scorer
# 시그니처: (matched:int, indicator_absent, unresolved:list, question:str, tbl_name:str) -> float
SCORE_FN = _default_score


def score_candidate(question: str, table_key: str, tbl_name: str = "") -> dict:
    """후보 표 1개를 getMeta로 펼쳐 feasible·score를 매긴다. 실패 시 feasible=False."""
    result = {"table_key": table_key, "tbl_name": tbl_name,
              "feasible": False, "score": 0.0, "indicator_absent": None, "unresolved": []}
    try:
        slots, _meta = build_slots(table_key, question)
    except Exception as e:  # noqa: BLE001 — 네트워크/메타 실패는 후보 탈락으로 처리
        result["error"] = str(e)[:120]
        return result

    absent = unresolved_indicator(question, slots, table_key)   # None=지표 있음
    unresolved = unresolved_qualifiers(question, slots)         # []=한정어 다 해소
    matched = sum(len(s.matched) for s in slots)

    result["indicator_absent"] = absent
    result["unresolved"] = unresolved
    result["feasible"] = (absent is None) and (not unresolved)
    # 점수는 교체 가능한 SCORE_FN이 산출(기본=_default_score). 값은 기존과 동일.
    result["score"] = SCORE_FN(matched, absent, unresolved, question, tbl_name)
    return result


def rank_candidates(question: str, cands: list[dict], limit: int = 8,
                    verbose: bool = False) -> list[dict]:
    """후보 dict 리스트를 재점수해 feasible→맥락→RRF 순으로 정렬."""
    scored: list[dict] = []
    for c in cands[:limit]:
        tk = c.get("table_key")
        if not tk:
            continue
        s = score_candidate(question, tk, c.get("tbl_name") or "")
        s["fusion_score"] = c.get("fusion_score")   # 검색기 RRF 점수(tie-break)
        s["final_rank"] = c.get("final_rank")        # 라이브 후보(RRF 없음) 정렬용
        if verbose:
            flag = "OK" if s["feasible"] else ("지표없음" if s["indicator_absent"]
                    else ("한정어미해소" if s["unresolved"] else "부적합"))
            print(f"    [재점수] {tk} rrf={s['fusion_score']} feasible={s['feasible']} ({flag}) {s['tbl_name'][:24]}")
        scored.append(s)
    # 정렬: feasible 우선 → 재점수(맥락: 실질/명목 등 도메인 신호) → RRF(tie-break) → table_key.
    # (RRF 단독 정렬은 라이브 후보에 RRF가 없고 실질/명목을 못 갈라 오선택 → 맥락을 주기준 유지)
    scored.sort(key=lambda x: (not x["feasible"], -x["score"],
                               -(x.get("fusion_score") or 0.0), x["table_key"]))
    return scored


def select_table(question: str, top_n: int = 20, rerank_k: int = 5,
                 verbose: bool = True) -> dict | None:
    """로컬 하이브리드 → getMeta 재점수 → (적합 표 없으면)KOSIS 라이브 폴백 → RAG 최종선택.

    반환 {table_key, tbl_name, reason, candidates} 또는 None(적합 표 없음 → 상위서 재질의/UNVERIFIABLE).
    """
    try:
        cands = map_question(question, top_n=top_n)
    except Exception as e:  # noqa: BLE001 — 로컬 검색기 실패해도 라이브 폴백으로 진행
        if verbose:
            print(f"  [로컬 검색 실패 → 라이브 폴백] {e}", file=sys.stderr)
        cands = []
    if verbose and cands:
        print("  [표 매핑] 로컬 하이브리드 후보:")
        for c in cands[:5]:
            print(f"     {c['final_rank']}위 {c['table_key']}  {c.get('tbl_name')}")

    ranked = rank_candidates(question, cands[:8], verbose=verbose)
    feasible = [r for r in ranked if r["feasible"]]
    source = "로컬"

    if not feasible:                       # 로컬에 적합 표 없음 → KOSIS 라이브 폭 확장
        live = search_tables_live(question, exclude={c["table_key"] for c in cands},
                                  k=LIVE_SEARCH_K, verbose=verbose)
        if verbose:
            print(f"  [라이브 폴백] KOSIS 검색 후보 {len(live)}개 중 상위 {LIVE_RESCORE_N}개 재점수")
        ranked_live = rank_candidates(question, live, limit=LIVE_RESCORE_N, verbose=verbose)
        feasible = [r for r in ranked_live if r["feasible"]]
        if feasible:
            source = "라이브"

    # 억지 채택 금지 — 지표·한정어가 실재하는(feasible) 표가 하나도 없으면 표를 반환하지 않는다.
    # (엉뚱한 표를 답변용으로 들고오는 현상 차단 → 상위에서 판단불가/재질의로 처리)
    if not feasible:
        if verbose:
            print("  [표 선택 실패] 지표·조건이 실재하는 표 없음 → 검증 불가")
        return None

    # 맥락 최고 그룹만 통과 — 같은 지표의 명목/실질·전국/도시 변형 중 주장에 맞는 것(예:'실질')만.
    # (리랭커 질의가 '실질' 같은 한정어를 떨궈 명목/실질을 못 가리는 경우의 안전망. feasible는
    #  재점수 주기준으로 정렬돼 feasible[0]가 맥락 최고점.)
    top = feasible[0]["score"]
    group = [r for r in feasible if r["score"] >= top - 0.01]
    short = [{"table_key": r["table_key"], "tbl_name": r["tbl_name"],
              "fusion_score": r.get("fusion_score"), "final_rank": i + 1}
             for i, r in enumerate(group[:rerank_k])]

    # 리랭커(NCP) 재정렬 — 질의를 '통계표 주제: 키워드'로 감싸서 넣는다(원문 문장은 인용 0건).
    # 인용 0건/실패 시 입력(RRF) 순서 유지 → 최악에도 기존 동작으로 수렴.
    if _RERANK and len(short) > 1:
        t_rr = time.perf_counter()
        try:
            from agent_reason import rerank, build_retrieval_query
            reranked = rerank(build_retrieval_query(question), short, verbose=verbose)
            if reranked:
                short = reranked
        except Exception as e:  # noqa: BLE001 — 리랭커 미동작 시 RRF 순서 유지
            print(f"  [리랭커 실패 → RRF 순서 유지] {e}", file=sys.stderr)
        if verbose:
            print(f"    [리랭커] {(time.perf_counter() - t_rr) * 1000:.0f}ms")

    # RAG Reasoning으로 최종 선택. 실패(예외)하거나 못 고르면(None) 상위(리랭커/RRF) 1위 폴백.
    table_key, reason = short[0]["table_key"], "(상위 1위)"
    try:
        from agent_reason import choose_table
        picked_key, why = choose_table(question, short, verbose=verbose)
        if picked_key:
            table_key, reason = picked_key, why
    except Exception as e:  # noqa: BLE001 — RAG 미동작 시 상위 1위 유지
        print(f"  [RAG Reasoning 실패 → 상위 1위] {e}", file=sys.stderr)

    picked = next((c for c in short if c["table_key"] == table_key), short[0])
    if verbose:
        print(f"  [표 선택] {table_key}  {picked.get('tbl_name')}  (경로:{source})")
    return {"table_key": table_key, "tbl_name": picked.get("tbl_name"),
            "reason": reason, "candidates": short}
