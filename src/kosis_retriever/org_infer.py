# -*- coding: utf-8 -*-
"""주장 → 생산기관(org) 집합 추론.
   HCX-007로 기관 판별 → ORG_MAP → 지표규칙 정규식 합집합. 디스크 캐시."""
import os, re, json, time, uuid, requests
from dotenv import load_dotenv
from .config import HCX_007, REPO, CACHE_DIR, ORG_MAP, INDICATOR_RULES

load_dotenv(REPO / ".env")
_KEY = os.environ.get("HCX_API_KEY")
_CACHE = CACHE_DIR / "org_cache.json"

SYS = ("당신은 뉴스 통계 주장을 보고, 그 수치를 생산·발표하는 KOSIS 기관을 판별합니다. "
       "주장에 인용된 출처가 있으면 그것을, 없으면 지표로 추정하세요. "
       "다음 org_id 중 하나로만 답하세요:\n"
       "101 = 통계청/국가데이터처 (인구·출생·사망·혼인·고용·취업·소비자물가·소비 등)\n"
       "301 = 한국은행 (GDP·국내총생산·경상수지·국제수지·금리·통화·외환·수출입물가·생산자물가)\n"
       "134 = 관세청 (수출입 무역통계·수출액·수입액·무역수지)\n"
       "110 = 기획재정부/행정안전부 (국세·세수)\n"
       "326 = 국토교통부 (건설·해외건설 수주·주택)\n"
       "기타 = 위에 해당 없음\n"
       'JSON {"org_id":"..."} 형식으로만 출력.')


def _load_cache():
    if _CACHE.exists():
        try: return json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}


def _save_cache(c):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _hcx_org(claim, cache):
    """HCX-007로 org_id 하나 판별(캐시)."""
    if claim in cache:
        return cache[claim]
    body = {"messages": [{"role": "system", "content": SYS}, {"role": "user", "content": f"주장:\n{claim}"}],
            "temperature": 0.0, "topP": 0.8, "topK": 0, "repetitionPenalty": 1.1, "maxCompletionTokens": 30,
            "responseFormat": {"type": "json", "schema": {"type": "object",
                               "properties": {"org_id": {"type": "string"}}, "required": ["org_id"]}},
            "thinking": {"effort": "none"}}
    val = None
    for attempt in range(5):
        try:
            r = requests.post(HCX_007, headers={"Authorization": f"Bearer {_KEY}",
                "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
                "Content-Type": "application/json", "Accept": "application/json"}, json=body, timeout=120)
            if r.status_code < 400:
                res = r.json().get("result", r.json()); c = res.get("message", {}).get("content", "")
                if isinstance(c, list): c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                c = str(c); s, e = c.find("{"), c.rfind("}")
                if s >= 0 and e > s:
                    val = str(json.loads(c[s:e + 1]).get("org_id") or "").strip()
                    break
        except Exception:
            pass
        time.sleep(min(2 ** attempt, 8))
    cache[claim] = val
    return val


def infer_orgs(claim):
    """주장 → org 집합. LLM 예측(ORG_MAP) ∪ 지표규칙 정규식."""
    cache = _load_cache()
    pred = _hcx_org(claim, cache)
    _save_cache(cache)
    orgs = set(ORG_MAP.get(pred, set()))
    for pattern, ids in INDICATOR_RULES:
        if re.search(pattern, claim):
            orgs |= ids
    return orgs
