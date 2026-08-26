# -*- coding: utf-8 -*-
"""주장 → HyDE 가상표 상상.
   G0 = 표이름만 / G2 = 표이름 + 분류값 형식. HCX-007, thinking=none, temp=0. 디스크 캐시."""
import os, json, time, uuid, requests
from dotenv import load_dotenv
from .config import HCX_007, REPO, CACHE_DIR

load_dotenv(REPO / ".env")
_KEY = os.environ.get("HCX_API_KEY")

# SYS_NAME(G0)·SYS_DIM(G2)
SYS_G0 = ("뉴스 주장에 대응할 KOSIS 통계표의 '표 이름'을 한 줄로만 추정해 출력한다. "
          "설명 없이 표 이름만. 통계청·한국은행 등 실제 KOSIS 표 명명 방식(예 '성/연령별 취업자', "
          "'수출입총괄', '외환보유액')처럼 간결하게.")
SYS_G2 = ("뉴스 주장에 대응할 KOSIS 통계표를 추정한다. 한 줄로 '표 이름'을 쓰고, 이어서 그 표가 가질 법한 "
          "분류축과 대표 항목을 콤마로 나열한다. 예: '성/연령별 취업자 | 성별, 연령대별, 취업자수, 고용률'. "
          "설명 문장 없이 이 형식만 출력.")


def _cache_path(tag):
    return CACHE_DIR / f"hyde_{tag}_cache.json"


def _load(tag):
    p = _cache_path(tag)
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}


def _save(tag, c):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(tag).write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _hcx(sys_prompt, claim):
    body = {"messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": claim}],
            "temperature": 0.0, "topP": 0.8, "topK": 0, "maxCompletionTokens": 100, "repetitionPenalty": 1.1,
            "thinking": {"effort": "none"}}
    for attempt in range(5):
        try:
            r = requests.post(HCX_007, headers={"Authorization": f"Bearer {_KEY}",
                "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
                "Content-Type": "application/json", "Accept": "application/json"}, json=body, timeout=120)
            if r.status_code < 400:
                res = r.json().get("result", r.json()); c = res.get("message", {}).get("content", "")
                if isinstance(c, list): c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                c = str(c).strip()
                if c:
                    return c
        except Exception:
            pass
        time.sleep(min(2 ** attempt, 8))
    return ""


def _generate(tag, sys_prompt, claim):
    cache = _load(tag)
    if claim in cache and cache[claim]:
        return cache[claim]
    pred = _hcx(sys_prompt, claim)
    cache[claim] = pred
    _save(tag, cache)
    return pred


def hyde_g0(claim):
    """표이름 상상."""
    return _generate("g0", SYS_G0, claim)


def hyde_g2(claim):
    """표이름 + 분류값 형식 상상."""
    return _generate("g2", SYS_G2, claim)
