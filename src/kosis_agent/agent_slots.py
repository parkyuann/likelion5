"""agent_slots.py — fetch_meta(실제 후보) → 재질의 슬롯 변환·결정론 매칭.

실전2 대화 흐름의 ②슬롯화 단계. kosis_call_tool.fetch_meta가 준 표 메타
(항목·분류축)를 agent_clarify.Slot 리스트로 만들고, 사용자 문장에 등장한
값을 **결정론적으로** matched에 채운다.

규칙:
  1) 구체적(숫자·분위·구간 포함) 축을 먼저 매칭하고 그 표면을 마스킹 → 뒷 축 이중매칭 방지
  2) 한 축에서 복수면 최장일치 우선(‘소득’ vs ‘근로소득’ → 긴 것)
  3) 그래도 복수면 주어(‘…<지표>(…)은/는 … 집계/줄/늘’) 현저성으로 1개 축소
  4) 끝내 못 줄이면 matched 비움 → 되묻지 않고 clarify 기본값(집계) 추론에 위임
분류값 선택을 사용자에게 되묻지 않는 것이 목표(값 결정은 에이전트 몫).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agent_clarify import Candidate, Slot, SlotKind, apply_answer, decide, Decision
from kosis_call_tool import TableMeta, fetch_meta

# 주어(지표) 명사 추출: '<명사>(부가)?은/는/이/가' — 주장이 무엇을 재는지
_SUBJECT_NOUN_RE = re.compile(r"([가-힣]{2,})(?:\([^)]*\))?(?:은|는|이|가)(?=[\s,.)]|$)")

_TBLNAME: dict[str, str] | None = None


def _tbl_name(table_key: str) -> str:
    """enriched600 카탈로그에서 표명 조회(1회 로드·캐시, 네트워크 무관). 없으면 ''."""
    global _TBLNAME
    if _TBLNAME is None:
        _TBLNAME = {}
        p = Path(__file__).resolve().parents[2] / "data" / "kosis_catalog_enriched_sample600.jsonl"
        if p.exists():
            for line in p.open(encoding="utf-8-sig"):
                if line.strip():
                    r = json.loads(line)
                    _TBLNAME[r.get("table_key")] = r.get("tbl_name") or ""
    return _TBLNAME.get(table_key, "")


def unresolved_indicator(question: str, slots: list[Slot], table_key: str = "") -> str | None:
    """주장 지표(주어 명사)가 이 표의 항목/분류값/표명 어디에도 없으면 그 지표를 반환.

    보수적: 주어 명사 중 하나라도 표 어휘에 부분일치하면 통과(None). 주어를 못 뽑아도 통과.
    반환값이 있으면 = 이 표엔 그 지표가 없다(→ 폴백해 다른 표로).
    """
    nouns = [m.group(1) for m in _SUBJECT_NOUN_RE.finditer(question or "")]
    if not nouns:
        return None
    vocab = [c.label for s in slots for c in s.candidates if c.label]
    tn = _tbl_name(table_key)
    if tn:
        vocab.append(tn)
    for n in nouns:
        if any(n in v or v in n for v in vocab):     # 양방향 부분일치(소비자물가↔물가 등)
            return None
    return nouns[-1]

# 주어(지표) 현저성: '<지표>(부가설명)?은/는/이/가' 형태로 서술의 주어가 되는 후보를 지표로 본다.
# 조사는 라벨(+괄호)에 '바로' 붙고 뒤가 경계여야 한다 — '가구'의 '가'를 주격조사로 오인식하지 않도록.
_SUBJECT_RE_TMPL = r"{}(?:\([^)]*\))?(?:은|는|이|가)(?=[\s,.)]|$)"
# 분위 표기: '3분위', '제 3 분위', '소득 3분위'
_QUANTILE_RE = re.compile(r"제?\s*(\d+)\s*분위")


def _find_hits(text: str, candidates: list[Candidate]) -> list[Candidate]:
    """text에 라벨이 부분일치하는 후보(최장일치 우선: 짧은 라벨이 긴 라벨의 부분이면 제거)."""
    hits = [c for c in candidates if c.label and c.label in text]
    if len(hits) <= 1:
        return hits
    by_len = sorted(hits, key=lambda c: -len(c.label))
    kept: list[Candidate] = []
    for c in by_len:
        if not any(c.label != k.label and c.label in k.label for k in kept):
            kept.append(c)
    return kept


def _salient(text: str, candidates: list[Candidate]) -> list[Candidate]:
    """후보 중 문장의 주어(‘…은/는’)에 해당하는 것(=지표)만 남긴다. 없으면 원본 반환."""
    subj = [c for c in candidates
            if re.search(_SUBJECT_RE_TMPL.format(re.escape(c.label)), text)]
    return subj if len(subj) == 1 else candidates


def _mask(text: str, labels: list[str]) -> str:
    """매칭된 표면 구간을 공백으로 가려 뒤 축이 같은 글자에 이중으로 걸리지 않게 한다."""
    for lb in labels:
        if lb:
            text = text.replace(lb, " " * len(lb))
    return text


def _numericish(slot: Slot) -> bool:
    """분위/구간/연령처럼 숫자·범위를 담는 축인가(먼저 매칭해 마스킹할 대상)."""
    return any(re.search(r"\d|분위|구간|~|이상|미만|이하", c.label) for c in slot.candidates)


def resolve_matches(question: str, slots: list[Slot]) -> None:
    """슬롯 전체를 구체성 순서로 결정론 매칭한다(부작용: slot.matched 채움)."""
    text = question or ""
    # 구체적(숫자류) 분류축 → 그 외 분류축 → 항목(itmId) 순서로 처리하며 마스킹
    order = sorted(
        range(len(slots)),
        key=lambda i: (0 if (slots[i].kind is SlotKind.DIMENSION and _numericish(slots[i]))
                       else 1 if slots[i].kind is SlotKind.DIMENSION else 2),
    )
    for i in order:
        slot = slots[i]
        hits = _find_hits(text, slot.candidates)
        if len(hits) >= 2:
            hits = _salient(text, hits)
        if len(hits) >= 2:            # 그래도 복수 → 되묻지 않으려 비움(기본값 추론에 위임)
            hits = []
        slot.matched = hits
        if hits:
            text = _mask(text, [c.label for c in hits])


def unresolved_qualifiers(question: str, slots: list[Slot]) -> list[str]:
    """주장에 명시됐지만 어느 축에도 못 붙은 한정어(예: '3분위'). 정합성 게이트용."""
    q = question or ""
    out: list[str] = []
    for m in _QUANTILE_RE.finditer(q):
        token = f"{m.group(1)}분위"
        # 어떤 분류축에 'N분위'(또는 '분위') 값이 존재하고 그게 matched됐는가?
        has_axis = any(
            s.kind is SlotKind.DIMENSION and any("분위" in c.label for c in s.candidates)
            for s in slots
        )
        matched_it = any(
            any(token in c.label or "분위" in c.label for c in s.matched)
            for s in slots
        )
        if not has_axis or not matched_it:
            out.append(token)
    return out


def _matched_by_text(question: str, candidates: list[Candidate]) -> list[Candidate]:
    """(하위호환) 단일 후보군 매칭 — 내부적으로 최장일치+현저성 규칙 사용."""
    hits = _find_hits(question or "", candidates)
    if len(hits) >= 2:
        hits = _salient(question or "", hits)
    return hits if len(hits) < 2 else []


def build_slots(table_key: str, question: str) -> tuple[list[Slot], TableMeta]:
    """fetch_meta → Slot 리스트(+결정론 매칭). 반환: (slots, meta)."""
    meta = fetch_meta(table_key)
    slots: list[Slot] = []

    # 항목 슬롯(itmId)
    item_cands = [Candidate(code=str(i), label=str(n)) for (i, n, _u) in meta.items]
    slots.append(Slot(name="항목", kind=SlotKind.ITEM, param="itmId",
                      candidates=item_cands, matched=[]))

    # 분류 슬롯(축마다). 1차원 표면 objL1 하나.
    for idx, dim in enumerate(meta.dimensions, start=1):
        dim_cands = [Candidate(code=v.code, label=v.label) for v in dim.values]
        slots.append(Slot(
            name=dim.obj_nm or f"분류{idx}", kind=SlotKind.DIMENSION, param=f"objL{idx}",
            candidates=dim_cands, matched=[],
            top_codes=tuple(v.code for v in dim.top_level),
        ))

    resolve_matches(question, slots)     # ← 결정론 매칭(과매칭 제거·현저성·마스킹)
    return slots, meta


def first_ask_slot(slots: list[Slot]) -> Slot | None:
    """지금 되물어야 하는(ASK) 첫 슬롯. (개선 후 분류값 ASK는 발생하지 않음 — 안전용 유지)"""
    ask = [s for s in slots if decide(s) is Decision.ASK]
    ask.sort(key=lambda s: 0 if s.kind is SlotKind.ITEM else 1)
    return ask[0] if ask else None


def apply_user_answer(slots: list[Slot], answer: str) -> bool:
    """현재 ASK 슬롯에 사용자 답을 반영. 성공 여부."""
    slot = first_ask_slot(slots)
    if slot is None:
        return False
    return apply_answer(slot, answer)
