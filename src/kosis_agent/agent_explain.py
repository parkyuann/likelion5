"""agent_explain.py — 판정 결과를 독자용 자연어 근거로 다듬는다(HCX-DASH-002).

결정론 엔진이 확정한 사실(표·항목·시점·실측값·오차·판정)만 받아, DASH-002가 **표현만**
자연스럽게 엮는다. **숫자·판정은 절대 바꾸지 않는다**(없는 수치 생성 금지) — 프로젝트 원칙
"설명은 KOSIS 원자료 근거 안에서만 생성" 준수. 실패 시 None을 돌려 상위가 템플릿을 유지.

호출 방식은 hcx_claim_experiment.call_hcx의 v3 chat-completions 패턴을 그대로 따른다.
"""
from __future__ import annotations

import os
import uuid

import requests

MODEL = "HCX-DASH-002"
URL = f"https://clovastudio.stream.ntruss.com/v3/chat-completions/{MODEL}"

_SYS = (
    "너는 통계 사실검증 결과를 독자에게 쉽게 설명하는 역할이다. "
    "아래 '사실'에 있는 숫자·판정을 절대 바꾸지 말고, 사실에 없는 수치는 지어내지 마라. "
    "2~4문장의 자연스러운 한국어로, 다음 순서로 설명하라: "
    "① 이 값이 KOSIS의 어느 통계표·항목·분류·시점에서 나온 것인지, "
    "② 실측값이 얼마인데 기사 원문은 그것을 어떻게 반올림해 표현했는지, "
    "③ 둘의 오차가 허용 범위 안(또는 밖)이라 왜 '일치'(또는 '불일치')로 볼 수 있는지. "
    "번호·머리말 없이 자연스러운 문단으로 쓴다."
)


def _api_key() -> str:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    key = (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("HCX_API_KEY 필요")
    return key


def _facts_text(f: dict) -> str:
    order = ["기관", "통계표", "항목·분류", "시점", "실측값", "비교시점값", "증감",
             "기사표현", "오차", "허용기준", "판정"]
    return "\n".join(f"- {k}: {f[k]}" for k in order if f.get(k))


def explain(facts: dict, timeout: int = 30) -> str | None:
    """facts(결정론 사실) → DASH-002가 다듬은 자연어 설명. 실패 시 None."""
    try:
        body = {
            "messages": [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": "사실:\n" + _facts_text(facts)},
            ],
            "temperature": 0.3, "topP": 0.8, "topK": 0,
            "repetitionPenalty": 1.1, "maxCompletionTokens": 400,
        }
        headers = {
            "Authorization": f"Bearer {_api_key()}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
            "Content-Type": "application/json", "Accept": "application/json",
        }
        res = requests.post(URL, headers=headers, json=body, timeout=timeout)
        if res.status_code >= 400:
            return None
        result = res.json().get("result") or {}
        msg = result.get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
        return content.strip() or None
    except Exception:  # noqa: BLE001 — 설명 실패는 치명적이지 않음(템플릿 유지)
        return None
