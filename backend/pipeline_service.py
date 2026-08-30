"""pipeline_service.py — 기존 파이프라인을 웹에서 부르기 쉬운 함수로 감싸는 얇은 층.

여기서 하는 일은 두 가지뿐이다.
  1) 기존 소스(src, src/kosis_agent)를 import 할 수 있게 경로를 잡아준다.
  2) FastAPI가 부르기 좋은 모양(순수 dict)으로 입출력을 정리한다.

새 검증 로직은 없다 — 전부 기존 함수(agent_pipeline.run, extract_from_article)를 호출한다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# --- 기존 소스 경로를 import 경로에 추가 -------------------------------------
# 이 파일: <root>/backend/pipeline_service.py  →  parents[1] == <root>
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
KOSIS_AGENT = SRC / "kosis_agent"
for path in (SRC, KOSIS_AGENT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

# .env(API 키) 로드 — 파이프라인이 HCX/KOSIS 키를 환경변수로 읽는다.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# 경로 설정 뒤에 import 해야 한다(순서 중요).
# 검증 로직은 현재 파이프라인 진입점(run_pipeline.factcheck_claim)을 사용한다.
import run_pipeline  # noqa: E402  (src)


def _jsonable(value: Any) -> Any:
    """op_result 같은 dataclass/객체가 섞여 있어도 JSON으로 내보낼 수 있게 정리."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    # dataclass 등 __dict__ 를 가진 객체는 속성 dict 로
    if hasattr(value, "__dict__"):
        return {str(k): _jsonable(v) for k, v in vars(value).items()}
    return str(value)


def verify_claim(
    text: str,
    explain: bool = True,
    *,
    pub_date: str | None = None,
) -> dict[str, Any]:
    """주장/질의 한 문장을 검증한다(현재 파이프라인 factcheck_claim 사용).

    반환: answer(자연어 답), action(answer|unverifiable), verdict(6분류 라벨),
          table_key, explanation(근거), url, 그리고 원본 결과(raw) 전체.
    """
    result = run_pipeline.factcheck_claim(text, pub_date, with_rationale=explain)
    table = result.get("table")
    # 근거: RAG 근거문이 있으면 그걸, 없으면 결정론 근거문(numeric)
    explanation = result.get("rationale") or result.get("numeric")
    return {
        "input": text,
        "answer": explanation,
        "action": "answer" if table else "unverifiable",
        "verdict": result.get("verdict"),
        "table_key": table,
        "explanation": explanation,
        "url": result.get("url"),
        "raw": _jsonable(result),
    }


def verify_article(
    text: str,
    title: str = "",
    date: str = "",
    max_claims: int | None = 10,
    explain: bool = False,
) -> dict[str, Any]:
    """기사 본문에서 검증 대상 주장을 추출하고, 각 주장을 검증해 목록으로 돌려준다.

    max_claims: LLM 호출이 주장 수만큼 발생하므로 데모 안전장치로 상한을 둔다(None=제한없음).
    explain=False: 기사 단위는 주장이 많아 기본적으로 자연어 설명 생성을 끈다(속도).
    """
    import claim_extractor  # noqa: E402 (src) — 기사→주장 추출
    rows = claim_extractor.extract_from_article(
        idx=0, title=title, date=date, label="", text=text
    )
    claims = [r for r in rows if r.get("claim_text")]
    if max_claims is not None:
        claims = claims[:max_claims]

    verified: list[dict[str, Any]] = []
    for row in claims:
        claim_text = row["claim_text"]
        try:
            one = verify_claim(
                claim_text,
                explain=explain,
                pub_date=(date or None),
            )
        except Exception as exc:  # 한 주장 실패가 전체를 막지 않도록
            one = {
                "input": claim_text,
                "answer": None,
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        one["sentence_index"] = row.get("sentence_index")
        one["sentence_char_start"] = row.get("sentence_char_start")
        one["sentence_char_end"] = row.get("sentence_char_end")
        char_start = row.get("sentence_char_start")
        char_end = row.get("sentence_char_end")
        one["evidence_text"] = (
            text[char_start:char_end]
            if isinstance(char_start, int) and isinstance(char_end, int)
            else claim_text
        )
        one["change_type"] = row.get("change_type")
        one["time_ref_abs"] = row.get("time_ref_abs")
        one["time_compare_abs"] = row.get("time_compare_abs")
        verified.append(one)

    failed_count = sum(item.get("action") == "error" for item in verified)
    if not claims:
        verification_status = "no_verifiable_claims"
    elif failed_count == len(verified):
        verification_status = "failed"
    elif failed_count:
        verification_status = "partial_failure"
    else:
        verification_status = "completed"

    return {
        "title": title,
        "date": date,
        "verification_status": verification_status,
        "claim_count": len(claims),
        "results": verified,
    }
