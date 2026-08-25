"""develop_verify_service.py — develop 배포 파이프라인(run_trace)을 웹에서 부르는 얇은 층.

옛 ``pipeline_service.py``와 같은 역할이지만 대상이 ``src/develop`` 배포 파이프라인
(``run_article_body_pipeline_trace_v1.run_trace``)이다.

하는 일:
  1) 요청마다 격리된 임시 입출력 디렉터리를 만들고 입력 계약(JSONL)을 기록한다.
  2) 인프라(Qdrant/인코더/리랭커 URL) 감지:
       - 셋 다 있으면 stage="all"        → 라이브 검색·판정까지(04)
       - 없으면 l1→l2→layers 순차 실행    → 구조화·라우팅까지(01~03)
  3) 출력을 프론트 표시 계약(segments)으로 투영한다.

인계서 요구: 내부 API 키·로컬 절대경로·원본 예외 문자열은 클라이언트 응답에 노출하지 않는다.
새 검증 로직은 없다 — 전부 파이프라인 함수(run_trace)를 호출한다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from backend.errors import BackendError

# --- 파이프라인 import 경로 --------------------------------------------------
# 이 파일: <root>/backend/develop_verify_service.py → parents[1] == <root>
# develop 진입점은 `from ..hcx_claim_experiment` 상대 import(=`src` 네임스페이스
# 패키지)와 일부 모듈의 bare import(`from claim_context_resolver ...`)를 함께 쓴다.
# 따라서 repo 루트(→ `src.develop`)와 `src`(→ bare import) 둘 다 sys.path에 둔다.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for _path in (str(ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# .env(API 키·서비스 URL) 로드 — 파이프라인이 환경변수로 읽는다.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

CONFIG_PATH = ROOT / "configs" / "pipeline_operational_v2.json"

# 라이브(stage 04)에 필요한 외부 서비스 엔드포인트. 셋 다 있어야 판정까지 수행.
_LIVE_ENV = ("QDRANT_URL", "BGE_QUERY_ENCODER_URL", "BGE_RERANKER_URL")

# 라이브 판정 verdict → 프론트 verdict 코드(VERDICTS 키)
_VERDICT_MAP = {"VERIFIED": "match", "REFUTED": "mismatch", "UNVERIFIABLE": "notfound"}

_PENDING_ANSWER = (
    "공식 통계(KOSIS) 검색·판정 인프라가 연결되면 자동으로 판정됩니다. "
    "현재는 검증 대상 문장과 검색 질의까지 준비했습니다."
)


def _live_ready() -> bool:
    return all((os.environ.get(name) or "").strip() for name in _LIVE_ENV)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _query_preview(targets: list[dict[str, Any]]) -> str:
    """라우팅이 생성한 대표 검색 질의(진단·표시용, 민감정보 없음)."""
    for target in targets:
        for query in target.get("retrieval_queries") or []:
            text = str(query.get("query") or "").strip()
            if text:
                return text
    return ""


def _live_table(ledger_row: dict[str, Any]) -> dict[str, Any] | None:
    """stage_ledger의 표 선택 정보를 EvidenceLink 계약으로 투영(라이브 전용, best-effort).

    실제 필드명은 인프라 연결 후 04_stage_ledger.jsonl로 최종 검증한다. 여기서는
    흔한 키 후보만 방어적으로 읽고, 표 정보를 못 찾으면 None을 돌려 링크를 생략한다.
    """
    table = ledger_row.get("table") if isinstance(ledger_row.get("table"), dict) else ledger_row
    org_id = table.get("org_id") or table.get("orgId")
    tbl_id = table.get("tbl_id") or table.get("tblId")
    name = table.get("table_name") or table.get("name") or table.get("title")
    period = table.get("period") or table.get("prd_de") or ""
    if not (name or (org_id and tbl_id)):
        return None
    projected: dict[str, Any] = {"name": str(name or f"{org_id}·{tbl_id}")}
    if org_id and tbl_id:
        projected["orgId"] = str(org_id)
        projected["tblId"] = str(tbl_id)
    if period:
        projected["path"] = str(period)
    return projected


def _sentence_segment(
    sid: Any,
    text: str,
    targets: list[dict[str, Any]],
    *,
    live: bool,
    answer_for_sentence: dict[Any, dict[str, Any]],
    ledger_for_sentence: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    if not live:
        # 배선(구조화)만: 검증 대상으로 식별된 문장을 '검증 불가능(대기)'로 표시.
        return {
            "text": text,
            "verifiable": True,
            "verdict": "notfound",
            "answer": _PENDING_ANSWER,
            "calc": (lambda q: f"검색 질의: {q}" if q else "")(_query_preview(targets)),
        }
    answer = answer_for_sentence.get(sid) or {}
    verdict = _VERDICT_MAP.get(str(answer.get("verdict") or "").upper(), "notfound")
    segment: dict[str, Any] = {
        "text": text,
        "verifiable": True,
        "verdict": verdict,
        "answer": str(answer.get("explanation") or answer.get("headline") or ""),
    }
    table = _live_table(ledger_for_sentence.get(sid) or {})
    if table:
        segment["table"] = table
    return segment


def _project_segments(out_root: Path, body: str, *, live: bool) -> list[dict[str, Any]]:
    """01_sentences(+라이브면 03/04)를 프론트 표시 계약(segments)으로 투영한다."""
    sentences = sorted(
        _read_jsonl(out_root / "01_sentences.jsonl"),
        key=lambda row: int(row.get("char_start") or 0),
    )
    routed = _read_jsonl(out_root / "03_routed.jsonl")
    targets_by_sentence: dict[Any, list[dict[str, Any]]] = {}
    for row in routed:
        targets_by_sentence.setdefault(row.get("article_sentence_id"), []).append(row)

    answer_for_sentence: dict[Any, dict[str, Any]] = {}
    ledger_for_sentence: dict[Any, dict[str, Any]] = {}
    if live:
        # target → 문장 매핑(라우팅이 보유). answer/ledger는 target 기준이므로
        # 문장 기준으로 되접는다. 필드명은 인프라 연결 후 최종 검증.
        target_to_sentence: dict[str, Any] = {}
        for row in routed:
            key = str(row.get("value_span_id") or row.get("target_id") or "")
            if key:
                target_to_sentence[key] = row.get("article_sentence_id")
        for row in _read_jsonl(out_root / "04_answers.jsonl"):
            key = str(row.get("target_id") or row.get("value_span_id") or "")
            sid = target_to_sentence.get(key, row.get("article_sentence_id"))
            answer_for_sentence.setdefault(sid, row)
        for row in _read_jsonl(out_root / "04_stage_ledger.jsonl"):
            key = str(row.get("target_id") or row.get("value_span_id") or "")
            sid = target_to_sentence.get(key, row.get("article_sentence_id"))
            ledger_for_sentence.setdefault(sid, row)

    segments: list[dict[str, Any]] = []
    cursor = 0
    length = len(body)
    for sentence in sentences:
        start = int(sentence.get("char_start") or 0)
        end = int(sentence.get("char_end") or 0)
        if not (0 <= start < end <= length):
            continue
        if start > cursor:
            segments.append({"text": body[cursor:start], "verifiable": False})
        seg_text = body[start:end]
        sid = sentence.get("sentence_id")
        sentence_targets = targets_by_sentence.get(sid) or []
        if sentence_targets:
            segments.append(
                _sentence_segment(
                    sid, seg_text, sentence_targets, live=live,
                    answer_for_sentence=answer_for_sentence,
                    ledger_for_sentence=ledger_for_sentence,
                )
            )
        else:
            segments.append({"text": seg_text, "verifiable": False})
        cursor = max(cursor, end)
    if cursor < length:
        segments.append({"text": body[cursor:], "verifiable": False})
    return segments


def verify_article_develop(text: str, title: str = "", date: str = "") -> dict[str, Any]:
    """기사 본문을 develop 파이프라인으로 검증하고 프론트 표시 계약으로 반환한다.

    반환: type/status/live/summary + results(segments 배열). 각 segment는
          {text, verifiable, [verdict, table, answer, calc]} 형태.
    """
    body = (text or "").strip()
    if not body:
        raise BackendError("ARTICLE_EMPTY", "검증할 기사 본문이 없습니다.", status_code=422)

    # 파이프라인 import는 무겁고 외부 의존(requests 등)을 끌어오므로 지연 로드한다.
    from src.develop.run_article_body_pipeline_trace_v1 import TraceStageError, run_trace

    live = _live_ready()
    article_id = uuid.uuid4().hex
    workdir = Path(tempfile.mkdtemp(prefix="verify_develop_"))
    articles_path = workdir / "articles.jsonl"
    out_root = workdir / "out"
    articles_path.write_text(
        json.dumps(
            {"article_idx": article_id, "title": title, "date": date, "article_text": body},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    def _value_claim_count() -> int:
        rows = _read_jsonl(out_root / "01_value_candidates.jsonl")
        return sum(1 for row in rows if row.get("kind") == "value_unit")

    try:
        # L1(결정론적·HCX 없음)로 검증 대상 수치 주장이 있는지 먼저 판별한다.
        run_trace(articles_path=articles_path, output_root=out_root, stage="l1")
        if _value_claim_count() == 0:
            # 수치 주장이 없으면 기사가 아니다(질문·잡담). 상위 라우터가 처리하도록 신호만 준다.
            return {"type": "not_article", "reason": "no_numeric_claims"}
        # 수치 주장이 있으면 나머지 단계를 진행한다(인프라 있으면 live까지).
        remaining = ["l2", "layers"] + (["live"] if live else [])
        for stage in remaining:
            run_trace(
                articles_path=articles_path,
                output_root=out_root,
                stage=stage,
                config_path=CONFIG_PATH if stage in ("all", "live") else None,
            )
        segments = _project_segments(out_root, body, live=live)
    except TraceStageError as exc:
        code = str(exc.args[0]) if exc.args else "PIPELINE_FAILED"
        raise BackendError(
            "VERIFY_PIPELINE_FAILED",
            "검증 파이프라인 처리에 실패했습니다.",
            status_code=502,
            detail={"stage_error": code[:120]},
        ) from None
    except Exception:
        # 원본 예외 문자열(경로·키 포함 가능)은 노출하지 않는다.
        raise BackendError(
            "VERIFY_PIPELINE_FAILED",
            "검증 파이프라인 처리 중 오류가 발생했습니다.",
            status_code=502,
        ) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    counts = {"match": 0, "mismatch": 0, "unverifiable": 0}
    for segment in segments:
        if not segment.get("verifiable"):
            continue
        verdict = segment.get("verdict")
        if verdict == "match":
            counts["match"] += 1
        elif verdict == "mismatch":
            counts["mismatch"] += 1
        else:
            counts["unverifiable"] += 1

    return {
        "type": "article",
        "status": "completed" if live else "structured_only",
        "live": live,
        "title": title,
        "date": date,
        "summary": counts,
        "results": segments,
    }
