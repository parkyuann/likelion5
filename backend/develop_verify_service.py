"""Backend entry point for the selectively packaged develop pipeline.

The only supported article path is the packaged ``src.develop`` trace runner.
Legacy ``src.run_pipeline`` and URL/image acquisition paths are never imported here.

하는 일:
  1) 요청마다 격리된 임시 입출력 디렉터리를 만들고 입력 계약(JSONL)을 기록한다.
  2) ``PIPELINE_LIVE_STAGE_ENABLED=true``일 때만 live stage를 추가한다.
     URL이 존재해도 false이면 항상 l1→l2→layers만 실행한다.
  3) 출력을 프론트 표시 계약(segments)으로 투영한다.

인계서 요구: 내부 API 키·로컬 절대경로·원본 예외 문자열은 클라이언트 응답에 노출하지 않는다.
새 검증 로직은 없다 — 전부 파이프라인 함수(run_trace)를 호출한다.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import date as calendar_date
from pathlib import Path
from typing import Any, Mapping

from backend.errors import BackendError
from backend.runtime_gate import pipeline_live_stage_enabled
from backend.verification_checkpoint_store import CheckpointError, consume as consume_checkpoint, create as create_checkpoint, discard as discard_checkpoint, read_option_page, update_context, validate_binding_continuation

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "pipeline_operational_v2.json"

# 라이브 판정 verdict → 프론트 verdict 코드(VERDICTS 키)
_VERDICT_MAP = {"VERIFIED": "match", "REFUTED": "mismatch", "UNVERIFIABLE": "notfound"}

_PENDING_ANSWER = (
    "공식 통계(KOSIS) 검색·판정 인프라가 연결되면 자동으로 판정됩니다. "
    "현재는 검증 대상 문장과 검색 질의까지 준비했습니다."
)

# 짧은 단문 '질문'을 기사 검증 이전에 걸러낸다. 질문에 든 나이·연도 숫자
# (예: "19세 이상 34세 이하")를 수치 주장으로 오인해 기사 검증으로 보내는 것을 막고,
# 질의는 상위 라우터(→ KOSIS MCP)로 넘긴다.
_QUESTION_MARKERS = (
    "얼마", "몇", "인가요", "인가", "일까", "될까", "할까", "한가요",
    "했나요", "되나요", "알려", "조회", "찾아", "어때", "어떤가",
    "무엇", "뭐야", "뭔가요", "있나요", "없나요",
)

_ARTICLE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD_ANSWER_PATTERN = re.compile(r"^(?:\d{4}|\d{4}-\d{2}|\d{4}년\s*[1-4]분기|\d{4}년\s*(?:0?[1-9]|1[0-2])월)$")
_DATE_SOURCES = {"user_feedback", "url_metadata", "api_request"}
_CLARIFICATION_ROLES = (
    "article_date", "period", "indicator", "item", "unit", "source", "population",
    "region", "sex", "age", "classification", "measurement_basis",
)
_CLARIFICATION_ROLE_PRIORITY = {role: index for index, role in enumerate(_CLARIFICATION_ROLES)}
_RELATIVE_PERIOD_PATTERN = re.compile(
    r"(?:지난해|작년|올해)(?:\s*\d{1,2}\s*(?:월|달|분기|반기))?"
    r"|(?:지난|이번|다음)\s*(?:\d{1,2}\s*)?(?:년|월|달|분기|반기)"
    r"|전년\s*(?:동월|동분기|월|분기)"
)


def _evidence_first_statistics_enabled_monthly_v2h() -> bool:
    return os.getenv("EVIDENCE_FIRST_STATISTICS_SHADOW_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def build_article_date_provenance_monthly_v2h(
    canonical_article_text: str,
) -> dict[str, str]:
    return {
        "date_source": "client_asserted",
        "source_path": "backend_request",
        "date_field": "date",
        "article_text_sha256": hashlib.sha256(
            canonical_article_text.encode("utf-8")
        ).hexdigest(),
    }


def normalize_article_date(date: str | None, date_source: str | None) -> tuple[str, str] | None:
    """Validate the explicit article date and apply the compatibility source default."""

    value = "" if date is None else date
    if not isinstance(value, str):
        raise BackendError("ARTICLE_DATE_INVALID", "기사 발행일은 YYYY-MM-DD 형식이어야 합니다.", status_code=422)
    if not value.strip():
        return None
    if not _ARTICLE_DATE_PATTERN.fullmatch(value):
        raise BackendError("ARTICLE_DATE_INVALID", "기사 발행일은 YYYY-MM-DD 형식이어야 합니다.", status_code=422)
    try:
        calendar_date.fromisoformat(value)
    except ValueError:
        raise BackendError("ARTICLE_DATE_INVALID", "기사 발행일은 실제 달력 날짜여야 합니다.", status_code=422) from None

    source = date_source or "api_request"
    if source not in _DATE_SOURCES:
        raise BackendError("ARTICLE_DATE_INVALID", "기사 발행일 출처가 올바르지 않습니다.", status_code=422)
    return value, source


def _validate_clarification_answers(value: Any) -> list[dict[str, str]]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 3:
        raise BackendError("CLARIFICATION_INVALID", "추가 입력은 최대 3회까지 허용됩니다.", status_code=422)
    result: list[dict[str, str]] = []
    for answer in value:
        if not isinstance(answer, dict):
            raise BackendError("CLARIFICATION_INVALID", "추가 입력 형식이 올바르지 않습니다.", status_code=422)
        question_id = str(answer.get("question_id") or "").strip()
        role = str(answer.get("role") or "").strip()
        raw = answer.get("value")
        text = raw.strip() if isinstance(raw, str) else ""
        option_id = str(answer.get("option_id") or "").strip()
        if role not in _CLARIFICATION_ROLES or not question_id or not text:
            raise BackendError("CLARIFICATION_INVALID", "추가 입력의 역할 또는 값이 올바르지 않습니다.", status_code=422)
        if role == "article_date":
            normalized = normalize_article_date(text, "user_feedback")
            assert normalized is not None
            text = normalized[0]
        elif role == "period":
            if not _PERIOD_ANSWER_PATTERN.fullmatch(text):
                raise BackendError("CLARIFICATION_INVALID", "시점은 YYYY, YYYY-MM, YYYY년 N월 또는 YYYY-QN 형식이어야 합니다.", status_code=422)
        elif not 1 <= len(text) <= 120:
            raise BackendError("CLARIFICATION_INVALID", "추가 입력은 1~120자의 자연어로 입력해 주세요.", status_code=422)
        record = {"question_id": question_id, "role": role, "value": text}
        if option_id:
            record["option_id"] = option_id
        result.append(record)
    return result


def _clarification_response(question: dict[str, Any], reason: str) -> dict[str, Any]:
    role = str(question.get("role") or "")
    if role not in _CLARIFICATION_ROLES:
        return {}
    question_id = str(question.get("id") or question.get("question_id") or "").strip()
    if not question_id:
        question_id = "cq-" + hashlib.sha256(
            json.dumps({"role": role, "prompt": question.get("prompt")}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
    input_mode = str(question.get("input_mode") or "FREE_TEXT")
    raw_options = question.get("options") if isinstance(question.get("options"), list) else []
    options: list[dict[str, Any]] = []
    for index, item in enumerate(raw_options):
        if isinstance(item, str):
            label = item
            item = {"label": label}
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("display_label") or "").strip()
        if not label:
            continue
        option_id = str(item.get("id") or item.get("option_id") or "").strip()
        if not option_id:
            option_id = "co-" + hashlib.sha256(f"{question_id}\0{index}\0{label}".encode("utf-8")).hexdigest()[:24]
        options.append({
            "id": option_id, "label": label,
            "description": str(item.get("description") or ""),
            "applicable_candidate_count": int(item.get("applicable_candidate_count") or len(item.get("applicability") or [])),
        })
    return {
        "type": "needs_user_input",
        "status": "awaiting_clarification",
        "reason": reason,
        "question": {
            "id": question_id,
            "role": role,
            "prompt": str(question.get("prompt") or "확인을 위해 통계 조건을 조금 더 알려주세요."),
            "input_mode": input_mode,
            "allow_direct_input": bool(question.get("allow_direct_input", input_mode in {"DATE", "FREE_TEXT", "SEARCHABLE_OPTIONS"})),
            "options": options[:20],
            "page": {
                "total": int(question.get("total") or len(options)),
                "limit": 20,
                "next_cursor": question.get("next_cursor"),
                "search_supported": input_mode == "SEARCHABLE_OPTIONS",
                "options_complete": not bool(question.get("next_cursor")),
            },
        },
        "clarification_receipt": {
            "contract_version": "clarification-plan-v2",
            "plan_sha256": str(question.get("plan_sha256") or ""),
            "candidate_membership_sha256": question.get("candidate_membership_sha256"),
            "profile_bundle_sha256": question.get("profile_bundle_sha256"),
            "speculative": bool(question.get("speculative")),
            "cell_api_calls": 0,
            "hcx_answer_calls": 0,
        },
    }


def _pre_live_clarification_plan(
    out_root: Path, *, body: str, article_date: str,
) -> dict[str, Any] | None:
    """Gate expensive retrieval when routed evidence cannot form a cell target."""
    routed = _read_jsonl(out_root / "03_routed.jsonl")
    if not routed:
        return None
    for row in routed:
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), dict) else {}
        period_text = " ".join(str(fields.get(key) or row.get(key) or "") for key in ("period_raw", "period", "period_context"))
        if not article_date and period_text and _RELATIVE_PERIOD_PATTERN.search(period_text):
            return {
                "reason": "ARTICLE_DATE_REQUIRED_FOR_RELATIVE_PERIOD",
                "question": {
                    "id": "cq-" + hashlib.sha256(f"article_date:{hashlib.sha256(body.encode('utf-8')).hexdigest()}".encode()).hexdigest()[:24],
                    "role": "article_date",
                    "prompt": "기사에서 말한 상대 시점을 정확한 통계 시점으로 바꾸려면 기사 발행일이 필요합니다. 기사 발행일을 YYYY-MM-DD 형식으로 알려주세요.",
                    "input_mode": "DATE", "allow_direct_input": True, "options": [], "speculative": False,
                },
                "resume_from_stage": "layers",
                "changed_roles": [], "invalidated_stages": ["layers", "retrieval", "binding", "cell", "answer"],
                "reusable_artifacts": ["l1", "l2", "layers"],
            }
    for row in routed:
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), dict) else {}
        if not fields and not any(key in row for key in ("indicator", "indicator_label", "period", "period_raw")):
            continue
        indicator = str(fields.get("indicator") or row.get("indicator") or row.get("indicator_label") or "").strip()
        item = fields.get("item") or row.get("item")
        indicator_missing = not indicator or indicator.casefold() in {"unknown", "ambiguous", "unavailable"}
        # Item is retrieval-critical only when L3~L5 marked an explicit item
        # family requirement; a normal indicator-only claim must still flow.
        item_required = bool(fields.get("item_required") or fields.get("requires_item") or row.get("item_required"))
        if indicator_missing or (item_required and item in (None, "", [], ())):
            role = "indicator" if indicator_missing else "item"
            return {
                "reason": f"{role.upper()}_REQUIRED",
                "question": {
                    "id": "cq-" + hashlib.sha256(f"{role}:{hashlib.sha256(body.encode('utf-8')).hexdigest()}".encode()).hexdigest()[:24],
                    "role": role,
                    "prompt": "어떤 통계 지표를 확인할지 알려주세요." if role == "indicator" else "어떤 통계 항목을 확인할지 알려주세요.",
                    "input_mode": "FREE_TEXT", "allow_direct_input": True, "options": [], "speculative": True,
                },
                "resume_from_stage": "layers",
                "changed_roles": [role], "invalidated_stages": ["layers", "retrieval", "binding", "cell", "answer"],
                "reusable_artifacts": ["l1", "l2", "layers"],
                "speculative": True,
            }
    return None


def _with_checkpoint(
    response: dict[str, Any], *, token: str, resume_from_stage: str,
) -> dict[str, Any]:
    return {**response, "resume_token": token, "resume_from_stage": resume_from_stage}


def _runtime_fingerprint() -> str:
    runtime_root = _pipeline_runtime_root()
    files = (
        runtime_root / "src" / "develop" / "run_article_body_pipeline_trace_v1.py",
        runtime_root / "src" / "news_verification" / "runtime" / "run_pipeline_operational_v2.py",
        ROOT / "backend" / "verification_checkpoint_store.py",
    )
    if any(not path.is_file() for path in files):
        return ""
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.resolve()).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _config_fingerprint() -> str:
    path = _pipeline_config_path()
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _pending_article_date_from_live(
    out_root: Path, *, body: str = "", article_date: str = "",
) -> dict[str, Any] | None:
    """Project date-provenance or relative-period holds into a date question."""
    for row in _read_jsonl(out_root / "04_stage_ledger.jsonl"):
        resolution = row.get("resolution")
        if isinstance(resolution, dict):
            code = str(resolution.get("hold_reason") or resolution.get("reason") or "")
        else:
            code = str(resolution or "")
        relative_period_without_date = (
            not article_date
            and code == "PERIOD_INVALID"
            and bool(_RELATIVE_PERIOD_PATTERN.search(body))
        )
        if code == "ARTICLE_DATE_PROVENANCE_INVALID" or relative_period_without_date:
            return _clarification_response(
                {
                    "role": "article_date",
                    "prompt": "기사의 상대 시점을 정확한 통계 시점으로 바꾸려면 기사 발행일이 필요합니다. 기사 발행일을 YYYY-MM-DD 형식으로 알려주세요.",
                    "input_mode": "DATE",
                    "options": [],
                },
                code,
            )
    return None


def _public_answer_text(value: Any) -> str:
    text = str(value or "")
    replacements = {
        "ARTICLE_DATE_PROVENANCE_INVALID": "기사 발행일을 기준으로 통계 시점을 확정할 수 없습니다.",
        "PERIOD_INVALID": "기사의 통계 시점을 현재 지원하는 형식으로 확정하지 못했습니다.",
        "PERIOD_UNSUPPORTED": "현재 지원하는 통계 주기로 확인하지 못했습니다.",
        "RANK_TIE_POLICY_PENDING": "동률 처리 기준이 없어 순위 주장은 확인하지 못했습니다.",
    }
    for code, message in replacements.items():
        text = text.replace(code, message)
    text = re.sub(
        r"\b(?:ARTICLE|PERIOD|ANNUAL|RANK|QUERY|PROFILE|CELL|RESUME)_[A-Z0-9_:-]+\b",
        "추가 근거를 확인하지 못했습니다.",
        text,
    )
    return text


def _pending_clarification(out_root: Path) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for row in _read_jsonl(out_root / "04_stage_ledger.jsonl"):
        plan = row.get("clarification_plan")
        if isinstance(plan, dict) and isinstance(plan.get("question"), dict):
            question = dict(plan["question"])
            question["candidate_membership_sha256"] = plan.get("candidate_membership_sha256")
            question["profile_bundle_sha256"] = plan.get("profile_bundle_sha256")
            question["speculative"] = bool(plan.get("speculative"))
            return _clarification_response(question, str(plan.get("reason") or "CLARIFICATION_REQUIRED"))
        recovery = row.get("failure_recovery_shadow")
        if not isinstance(recovery, dict):
            continue
        # Once v2 planning is enabled, a legacy string-only question is not
        # safe to resume because its option/question binding is unverifiable.
        question = recovery.get("question")
        if not isinstance(question, dict):
            post_retry = recovery.get("post_retry")
            question = post_retry.get("question") if isinstance(post_retry, dict) else None
        if not isinstance(question, dict):
            continue
        if recovery.get("contract_version") != "clarification-plan-v2":
            raise BackendError(
                "CLARIFICATION_PLAN_INVALID",
                "이전 재질의 정보의 검증 계약이 만료되었습니다. 원문을 다시 제출해 주세요.",
                status_code=409,
            )
        role = str(question.get("role") or "")
        if role not in _CLARIFICATION_ROLES:
            continue
        post_retry = recovery.get("post_retry")
        retry_reason = post_retry.get("reason") if isinstance(post_retry, dict) else ""
        reason = str(recovery.get("reason") or retry_reason or row.get("resolution") or "CLARIFICATION_REQUIRED")
        candidates.append((_CLARIFICATION_ROLE_PRIORITY[role], question, reason))
    if not candidates:
        return None
    _, question, reason = sorted(candidates, key=lambda item: item[0])[0]
    return _clarification_response(question, reason)


def _pending_clarification_plan(out_root: Path) -> dict[str, Any] | None:
    """Return the sealed v2 plan for checkpoint creation, not its public view."""
    for row in _read_jsonl(out_root / "04_stage_ledger.jsonl"):
        plan = row.get("clarification_plan")
        if isinstance(plan, dict) and plan.get("contract_version") == "clarification-plan-v2":
            if isinstance(plan.get("question"), dict):
                return dict(plan)
    return None


def _iter_period_field_texts(value: Any):
    """Yield only text nested under a routed period field."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_period_field_texts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_period_field_texts(nested)


def _routed_relative_period_requires_article_date(out_root: Path) -> bool:
    """Detect a relative routed period when the live ledger emitted no ASK_USER."""
    for row in _read_jsonl(out_root / "03_routed.jsonl"):
        containers = [row]
        for key in ("retrieval_fields", "routed_fields", "fields"):
            value = row.get(key)
            if isinstance(value, dict):
                containers.append(value)
        for container in containers:
            for key in ("period", "period_raw", "period_context"):
                for text in _iter_period_field_texts(container.get(key)):
                    if _RELATIVE_PERIOD_PATTERN.search(text):
                        return True
    return False


def _pending_article_date_from_routed(out_root: Path) -> dict[str, Any] | None:
    if not _routed_relative_period_requires_article_date(out_root):
        return None
    return _clarification_response(
        {
            "role": "article_date",
            "prompt": "기사의 '지난 4월'과 같은 상대 시점을 정확한 통계 시점으로 바꾸려면 기사 발행일이 필요합니다. 기사 발행일을 YYYY-MM-DD 형식으로 알려주세요.",
            "input_mode": "DATE",
            "options": [],
        },
        "ARTICLE_DATE_REQUIRED_FOR_RELATIVE_PERIOD",
    )


def _looks_like_question(text: str) -> bool:
    """전체 입력이 짧은 통계 '질문'인지 보수적으로 판별한다(기사 오인 방지)."""
    if "\n" in text:
        return False  # 여러 줄이면 기사로 본다
    normalized = " ".join(text.split())
    if not normalized or len(normalized) > 200:
        return False  # 길면 기사로 본다
    if normalized.endswith("?"):
        return True
    return any(marker in normalized for marker in _QUESTION_MARKERS)


def _pipeline_runtime_root() -> Path:
    configured = os.getenv("PIPELINE_RUNTIME_ROOT", "").strip()
    return Path(configured) if configured else ROOT / "pipeline_runtime"


def _pipeline_config_path() -> Path:
    configured = os.getenv("PIPELINE_CONFIG_PATH", "").strip()
    return Path(configured).resolve() if configured else CONFIG_PATH


def _load_trace_runner() -> tuple[Any, type[Exception]]:
    """Load only the packaged closure, never the repository's legacy root modules."""

    runtime_root = _pipeline_runtime_root()
    if not (runtime_root / "src" / "develop" / "run_article_body_pipeline_trace_v1.py").is_file():
        raise BackendError(
            "PIPELINE_RUNTIME_SOURCE_PENDING",
            "배포 이미지에 승인된 pipeline runtime closure가 없습니다.",
            status_code=503,
        )
    import importlib
    import sys

    root_text = str(runtime_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        module = importlib.import_module("src.develop.run_article_body_pipeline_trace_v1")
        # Keep the public loader tuple backward compatible while exposing the
        # continuation primitive to the service.
        setattr(module.run_trace, "_prepare_resume", module.prepare_resume)
        runtime_module = importlib.import_module("src.news_verification.runtime.run_pipeline_operational_v2")
        setattr(module.run_trace, "_run_speculative", runtime_module.run_live_from_files)
        return module.run_trace, module.TraceStageError
    except Exception as exc:
        raise BackendError(
            "PIPELINE_RUNTIME_SOURCE_PENDING",
            "승인된 pipeline runtime closure를 불러올 수 없습니다.",
            status_code=503,
        ) from exc


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


_ROUTED_FAMILY_SUFFIXES = (
    "증가율", "감소율", "상승률", "하락률", "증감률", "변화율", "변동률",
    "증가량", "감소량", "상승량", "하락량", "증감량", "변화량", "변동량",
    "증가폭", "감소폭", "상승폭", "하락폭", "증감폭", "변화폭", "변동폭",
    "증가 폭", "감소 폭", "상승 폭", "하락 폭", "증감 폭", "변화 폭", "변동 폭",
    "건수", "규모", "수준", "수",
)
_ROUTED_FREQUENCY_PREFIX_RE = re.compile(r"^(?:월별)\s+")


def _routed_indicator_family_key(value: Any) -> str:
    """Normalize closed measurement-role suffixes for article-wide grouping."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _ROUTED_FREQUENCY_PREFIX_RE.sub("", text)
    while text:
        for suffix in _ROUTED_FAMILY_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                break
        else:
            break
    return text


def _deterministic_claim_query_from_routed(out_root: Path) -> str | None:
    """Build a single-family live selector query; preserve multi-family rows."""
    routed_rows = _read_jsonl(out_root / "03_routed.jsonl")
    families: set[str] = set()
    for row in routed_rows:
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), dict) else {}
        indicator = fields.get("indicator") or row.get("indicator") or row.get("indicator_label")
        family = _routed_indicator_family_key(indicator)
        if family:
            families.add(family)
    if len(families) > 1:
        return None

    for row in routed_rows:
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), dict) else {}
        measurement_type = str(
            fields.get("measurement_type") or row.get("measurement_type") or ""
        ).strip().upper()
        if measurement_type != "LEVEL":
            continue
        indicator = str(fields.get("indicator") or row.get("indicator") or "").strip()
        period = fields.get("period_absolute") or fields.get("period_raw") or row.get("period_raw") or ""
        if isinstance(period, dict):
            period = period.get("absolute") or period.get("raw") or ""
        period_text = str(period).strip()
        query = " ".join(part for part in (indicator, period_text) if part)
        if query:
            return query
    return None


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
        "answer": _public_answer_text(answer.get("explanation") or answer.get("headline") or ""),
    }
    evidence_answer = answer.get("evidence_answer")
    if isinstance(evidence_answer, dict) and str(evidence_answer.get("text") or "").strip():
        segment["evidence_answer"] = evidence_answer
        segment["answer"] = _public_answer_text(evidence_answer["text"])
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
            value_span_id = str(row.get("value_span_id") or "")
            if value_span_id:
                sentence_id = row.get("article_sentence_id")
                target_to_sentence[value_span_id] = sentence_id
                article_idx = str(row.get("article_idx") or "")
                if article_idx:
                    target_to_sentence[f"{article_idx}:{value_span_id}"] = sentence_id
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


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _receipt_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _routed_target_keys(row: dict[str, Any]) -> tuple[str, ...]:
    article_idx = str(row.get("article_idx") or "")
    value_span_id = str(row.get("value_span_id") or row.get("sentence_id") or "target")
    values = [str(row.get("target_id") or ""), f"{article_idx}:{value_span_id}", value_span_id]
    return tuple(value for value in values if value)


def _strict_iso_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = calendar_date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _bounded_public_error_code(value: Any) -> str | None:
    code = str(value or "")
    return code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", code) else None


def _official_cell_evidence(target_id: str, ledger: dict[str, Any]) -> tuple[bool, str | None]:
    """Accept only a complete, target-bound canonical cell receipt."""
    resolution = ledger.get("resolution")
    selected = ledger.get("selected_table")
    cell = ledger.get("cell")
    query_plan = ledger.get("query_plan")
    call_ledger = ledger.get("call_ledger")
    expected_release_id = os.getenv("KOSIS_RELEASE_ID", "")
    if not expected_release_id:
        return False, "RELEASE_ID_UNAVAILABLE"
    if not isinstance(resolution, dict) or resolution.get("outcome") != "QUERY_READY":
        return False, "QUERY_NOT_READY"
    if not isinstance(selected, dict) or not str(selected.get("table_key") or ""):
        return False, "SELECTED_TABLE_MISSING"
    if str(selected.get("release_id") or "") != expected_release_id:
        return False, "RELEASE_ID_MISMATCH"
    profile_sha = str(selected.get("profile_sha256") or "")
    if not _SHA256_RE.fullmatch(profile_sha):
        return False, "PROFILE_RECEIPT_INVALID"
    if _strict_iso_date(selected.get("send_de")) is None:
        return False, "SEND_DE_INVALID"
    if not isinstance(query_plan, dict):
        return False, "CELL_SELECTOR_MISSING"
    required_selector = ("org_id", "tbl_id", "itm_id", "prd_se", "start_prd_de", "end_prd_de", "obj_levels")
    if any(not query_plan.get(key) for key in required_selector) or not isinstance(query_plan.get("obj_levels"), dict):
        return False, "CELL_SELECTOR_MISSING"
    query_sha = _receipt_sha256(query_plan)
    if str(selected.get("query_plan_sha256") or "") != query_sha:
        return False, "CELL_SELECTOR_IDENTITY_INVALID"
    if str(ledger.get("target_id") or "") != target_id:
        return False, "TARGET_IDENTITY_INVALID"
    if not isinstance(call_ledger, dict) or not isinstance(call_ledger.get("cell_api"), int) or call_ledger["cell_api"] < 1:
        return False, "CELL_CALL_RECEIPT_MISSING"
    if not isinstance(cell, dict) or cell.get("status") != "CELL_RESOLVED":
        return False, "CELL_NOT_RESOLVED"
    if not _SHA256_RE.fullmatch(str(cell.get("response_sha256") or "")):
        return False, "CELL_RESPONSE_RECEIPT_INVALID"
    if not isinstance(cell.get("query"), dict) or _receipt_sha256(cell["query"]) != query_sha:
        return False, "CELL_SELECTOR_IDENTITY_INVALID"
    official_cell = cell.get("cell")
    if not isinstance(official_cell, dict) or str(official_cell.get("DT") or "").strip() == "":
        return False, "OFFICIAL_VALUE_MISSING"
    if not str(ledger.get("official_unit") or "").strip():
        return False, "OFFICIAL_UNIT_MISSING"
    return True, None


def _target_receipts(out_root: Path) -> list[dict[str, Any]]:
    """Project one bounded, public receipt for every routed live target."""
    routed = _read_jsonl(out_root / "03_routed.jsonl")
    ledgers = _read_jsonl(out_root / "04_stage_ledger.jsonl")
    by_target: dict[str, dict[str, Any]] = {}
    for ledger in ledgers:
        for key in _routed_target_keys(ledger):
            by_target[key] = ledger

    receipts: list[dict[str, Any]] = []
    for row in routed:
        keys = _routed_target_keys(row)
        target_id = keys[1] if len(keys) > 1 else (keys[0] if keys else "")
        ledger = next((by_target[key] for key in keys if key in by_target), {})
        fields = row.get("retrieval_fields") if isinstance(row.get("retrieval_fields"), dict) else {}
        resolution = ledger.get("resolution")
        outcome = resolution.get("outcome") if isinstance(resolution, dict) else None
        cell = ledger.get("cell") if isinstance(ledger.get("cell"), dict) else {}
        retrieval = ledger.get("retrieval") if isinstance(ledger.get("retrieval"), dict) else {}
        top_level_candidates = ledger.get("candidate_membership")
        if isinstance(top_level_candidates, list):
            candidates = top_level_candidates
        else:
            candidates = retrieval.get("candidate_membership")
            if not isinstance(candidates, list):
                candidates = retrieval.get("candidate_table_keys")
            if not isinstance(candidates, list):
                candidates = []
        candidate_table_keys = []
        for candidate in candidates:
            table_key = (
                candidate
                if isinstance(candidate, str)
                else candidate.get("table_key") if isinstance(candidate, dict)
                else getattr(candidate, "table_key", None)
            )
            text = str(table_key or "").strip()
            if text:
                candidate_table_keys.append(text)
        retrieval_calls = retrieval.get("calls")
        retrieval_calls = retrieval_calls if isinstance(retrieval_calls, int) and not isinstance(retrieval_calls, bool) else None
        channel_calls = retrieval.get("channel_calls")
        if isinstance(channel_calls, dict):
            channel_calls = {
                str(key): value
                for key, value in channel_calls.items()
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            }
        else:
            channel_calls = {}
        raw_channel_audits = retrieval.get("channel_audits")
        channel_audits: dict[str, list[dict[str, Any]]] = {}
        if isinstance(raw_channel_audits, dict):
            for name, raw_events in raw_channel_audits.items():
                if not isinstance(raw_events, list):
                    continue
                bounded_events: list[dict[str, Any]] = []
                for raw_event in raw_events:
                    if not isinstance(raw_event, dict):
                        continue
                    query_sha256 = raw_event.get("query_sha256")
                    status = raw_event.get("boundary_status")
                    if not isinstance(query_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", query_sha256):
                        continue
                    if status not in {"CLOSED", "DROPPED_UNCLOSED_CUTOFF_TIE"}:
                        continue
                    event: dict[str, Any] = {"query_sha256": query_sha256, "boundary_status": status}
                    for key in ("cutoff_score", "observed_tied_count", "requested_window"):
                        value = raw_event.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            event[key] = value
                    expansions = raw_event.get("expansions")
                    if isinstance(expansions, list) and all(
                        isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        for value in expansions
                    ):
                        event["expansions"] = expansions[:16]
                    if set(event) == {
                        "query_sha256", "boundary_status", "cutoff_score",
                        "observed_tied_count", "requested_window", "expansions",
                    }:
                        bounded_events.append(event)
                if bounded_events:
                    channel_audits[str(name)] = sorted(
                        bounded_events,
                        key=lambda event: json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    )
        metadata_calls = ledger.get("metadata_api_calls")
        metadata_calls = metadata_calls if isinstance(metadata_calls, int) and not isinstance(metadata_calls, bool) and metadata_calls >= 0 else None
        metadata_lookups = ledger.get("metadata_lookups")
        metadata_lookups = metadata_lookups if isinstance(metadata_lookups, int) and not isinstance(metadata_lookups, bool) and metadata_lookups >= 0 else None
        failure = ledger.get("failure") if isinstance(ledger.get("failure"), dict) else {}
        failure_code = _bounded_public_error_code(failure.get("error_code"))
        query_plan = ledger.get("query_plan") if isinstance(ledger.get("query_plan"), dict) else {}
        selected = ledger.get("selected_table") if isinstance(ledger.get("selected_table"), dict) else {}
        call_ledger = ledger.get("call_ledger") if isinstance(ledger.get("call_ledger"), dict) else {}
        ledger_target_id = str(ledger.get("target_id") or target_id)
        official, limitation_code = _official_cell_evidence(ledger_target_id, ledger)
        limitation = {
            "error_code": failure_code,
            "call_delta": {
                "retrieval": retrieval_calls,
                "channel_calls": channel_calls,
                "metadata_api": metadata_calls,
                "metadata_lookups": metadata_lookups,
            },
        } if not official and (
            failure_code or retrieval_calls is not None or channel_calls
            or metadata_calls is not None or metadata_lookups is not None
        ) else None
        receipts.append({
            "article_idx": str(row.get("article_idx") or ""),
            "target_id": ledger_target_id,
            "sentence_id": row.get("article_sentence_id"),
            "value_span_id": row.get("value_span_id"),
            "measurement_type": fields.get("measurement_type") or row.get("measurement_type"),
            "indicator": fields.get("indicator") or row.get("indicator") or row.get("indicator_label"),
            "period": fields.get("period") or fields.get("period_absolute") or row.get("period_raw"),
            "unit": fields.get("unit") or fields.get("unit_raw") or row.get("value_unit"),
            "region": fields.get("region") or fields.get("region_raw"),
            "retrieval": {
                "calls": retrieval_calls,
                "channel_calls": channel_calls,
                "channel_audits": channel_audits,
                "candidate_table_keys": candidate_table_keys,
            },
            "metadata_binding": {
                "calls": metadata_calls,
                "lookups": metadata_lookups,
                "compatible_table_keys": [str(value) for value in (resolution.get("compatible_series") or [])]
                if isinstance(resolution, dict) else [],
            },
            "selected_table_key": selected.get("table_key"),
            "send_de": selected.get("send_de"),
            "release_id": selected.get("release_id"),
            "profile_sha256": selected.get("profile_sha256"),
            "query_ready": outcome == "QUERY_READY",
            "cell": {
                "calls": call_ledger.get("cell_api") if isinstance(call_ledger.get("cell_api"), int) else 0,
                "response_sha256": cell.get("response_sha256"),
                "official_value": (cell.get("cell") or {}).get("DT") if isinstance(cell.get("cell"), dict) else None,
                "official_unit": ledger.get("official_unit"),
                "period": query_plan.get("start_prd_de") or query_plan.get("end_prd_de"),
                "status": cell.get("status"),
            },
            "official_cell_evidence": official,
            "terminal_status": "official_cell" if official else str(
                (resolution.get("hold_reason") or resolution.get("outcome"))
                if isinstance(resolution, dict) else resolution or "UNVERIFIABLE"
            ),
            "limitation_code": limitation_code or (
                None if official else str(
                    (resolution.get("hold_reason") or resolution.get("outcome"))
                    if isinstance(resolution, dict) else resolution or "UNVERIFIABLE"
                )
            ),
            "limitation": limitation,
        })
    return receipts


def _live_status(out_root: Path) -> tuple[str, list[dict[str, Any]]]:
    """Derive the article status from per-target official-cell evidence."""
    receipts = _target_receipts(out_root)
    official = sum(1 for row in receipts if row["official_cell_evidence"])
    if official == len(receipts) and official > 0:
        return "completed", receipts
    if official > 0:
        return "completed_with_limits", receipts
    return "unverifiable", receipts


def get_clarification_options(
    resume_token: str,
    *,
    question_id: str,
    query: str = "",
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    if os.getenv("PIPELINE_CLARIFICATION_OPTIONS_ENABLED", "true").strip().lower() == "false":
        raise BackendError("CLARIFICATION_OPTIONS_DISABLED", "추가 선택지 조회 기능이 비활성화되어 있습니다.", status_code=409)
    try:
        return read_option_page(
            resume_token, question_id=question_id, query=query, cursor=cursor, limit=limit,
        )
    except CheckpointError as exc:
        status = 422 if exc.code.endswith("LIMIT_INVALID") else 409
        raise BackendError(exc.code, "추가 선택지를 조회할 수 없습니다.", status_code=status) from None


def verify_article_develop(
    text: str,
    title: str = "",
    date: str | None = "",
    date_source: str | None = None,
    clarification_answers: Any = None,
    resume_token: str | None = None,
) -> dict[str, Any]:
    """기사 본문을 develop 파이프라인으로 검증하고 프론트 표시 계약으로 반환한다.

    반환: type/status/live/summary + results(segments 배열). 각 segment는
          {text, verifiable, [verdict, table, answer, calc]} 형태.
    """
    body = (text or "").strip()
    if not body:
        raise BackendError("ARTICLE_EMPTY", "검증할 기사 본문이 없습니다.", status_code=422)

    # 짧은 통계 질문이면 기사 검증 대상이 아니다. 나이·연도 숫자를 수치 주장으로
    # 오인하지 않도록 파이프라인 이전에 걸러 상위 라우터(→ KOSIS MCP)로 넘긴다.
    if _looks_like_question(body):
        return {"type": "not_article", "reason": "question"}

    clarification_history = _validate_clarification_answers(clarification_answers)
    if clarification_history and not resume_token:
        raise BackendError(
            "RESUME_CHECKPOINT_REQUIRED",
            "추가 입력을 이어서 처리할 검증 체크포인트가 없습니다. 원문을 다시 제출해 주세요.",
            status_code=409,
        )
    evidence_first_statistics = _evidence_first_statistics_enabled_monthly_v2h()
    normalized_date = normalize_article_date(
        date, None if evidence_first_statistics else date_source
    )
    article_date = normalized_date[0] if normalized_date is not None else ""
    article_date_source = normalized_date[1] if normalized_date is not None else None
    date_answers = [answer for answer in clarification_history if answer["role"] == "article_date"]
    if date_answers:
        if article_date and article_date != date_answers[-1]["value"]:
            raise BackendError("CLARIFICATION_CONFLICT", "기존 기사 발행일과 추가 입력이 서로 다릅니다.", status_code=422)
        article_date = date_answers[-1]["value"]
        article_date_source = "user_feedback"

    # PENDING 경로가 먼저 닫힌 뒤, explicit article만 승인된 closure를 지연 로드한다.
    run_trace, trace_stage_error = _load_trace_runner()
    live = pipeline_live_stage_enabled()
    article_body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    config_sha256 = _config_fingerprint()
    runtime_fingerprint = _runtime_fingerprint()
    supplied_title = title.strip() or "제목 미상 기사"
    checkpoint = None
    if resume_token:
        try:
            checkpoint = consume_checkpoint(
                resume_token,
                article_body_sha256=article_body_sha256,
                title=supplied_title,
                clarification_history=clarification_history,
                runtime_fingerprint=runtime_fingerprint,
                config_sha256=config_sha256,
                expected_question_id=clarification_history[-1].get("question_id") if clarification_history else None,
                expected_role=clarification_history[-1].get("role") if clarification_history else None,
            )
        except CheckpointError as exc:
            raise BackendError(
                exc.code,
                "이전 검증을 이어서 처리할 수 없습니다. 원문을 다시 제출해 주세요.",
                status_code=409,
            ) from None
        workdir = checkpoint.root
        articles_path = checkpoint.article_path
        out_root = checkpoint.output_root
        article_id = str(checkpoint.metadata.get("article_id") or "")
        resume_from_stage = str(checkpoint.metadata.get("resume_from_stage") or "")
        if resume_from_stage == "binding":
            # The current Gate-B checkpoint does not yet contain complete
            # candidate/profile bytes, only their receipts.  Never disguise a
            # full live rerun as binding continuation.
            if os.getenv("PIPELINE_BINDING_RESUME_ENABLED", "true").strip().lower() != "true":
                raise BackendError(
                    "BINDING_RESUME_PENDING",
                    "선택 조건을 안전하게 이어 처리할 봉인 산출물이 아직 준비되지 않았습니다. 원문을 다시 제출해 주세요.",
                    status_code=409,
                )
            try:
                validate_binding_continuation(
                    checkpoint,
                    expected_release_id=os.getenv("KOSIS_RELEASE_ID", "").strip() or None,
                )
            except CheckpointError:
                raise BackendError(
                    "RESUME_ARTIFACT_INVALIDATED",
                    "검증 재개 산출물의 후보·profile·release 봉인이 일치하지 않습니다.",
                    status_code=409,
                ) from None
        update_context(checkpoint, clarification_history)
    else:
        article_id = uuid.uuid4().hex
        workdir = Path(tempfile.mkdtemp(prefix="verify_develop_"))
        articles_path = workdir / "articles.jsonl"
        out_root = workdir / "out"
        articles_path.write_text(
            json.dumps(
                {
                    "article_idx": article_id,
                    "title": supplied_title,
                    "date": article_date,
                    "article_text": body,
                    "article_date_provenance": (
                        build_article_date_provenance_monthly_v2h(body)
                        if evidence_first_statistics else
                        {
                            "date_source": article_date_source,
                            "date_field": "date",
                            "article_text_sha256": article_body_sha256,
                        }
                    ),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        resume_from_stage = ""
    preserve_workdir = False
    live_status = "structured_only"
    target_receipts: list[dict[str, Any]] = []
    stage_timings: dict[str, dict[str, int]] = {}

    def _value_claim_count() -> int:
        rows = _read_jsonl(out_root / "01_value_candidates.jsonl")
        return sum(1 for row in rows if row.get("kind") == "value_unit")

    def _make_pending_checkpoint(
        pending: dict[str, Any], resume_from: str,
        plan_override: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = dict(plan_override or {
            "contract_version": "clarification-plan-v2",
            "reason": str(pending.get("reason") or "CLARIFICATION_REQUIRED"),
            "question": dict(pending.get("question") or {}),
            "candidate_membership_sha256": pending.get("candidate_membership_sha256"),
            "profile_bundle_sha256": pending.get("profile_bundle_sha256"),
            "speculative": bool(pending.get("speculative")),
        })
        plan.setdefault("contract_version", "clarification-plan-v2")
        plan.setdefault("reason", str(pending.get("reason") or "CLARIFICATION_REQUIRED"))
        plan["question"] = dict(plan.get("question") or pending.get("question") or {})
        question = plan["question"]
        question.setdefault("id", "cq-" + uuid.uuid4().hex)
        question.setdefault("role", "period")
        question.setdefault("prompt", "확인을 위해 통계 조건을 조금 더 알려주세요.")
        question.setdefault("input_mode", "FREE_TEXT")
        options = question.get("options") if isinstance(question.get("options"), list) else []
        bundle_options = []
        for index, option in enumerate(options):
            if isinstance(option, str):
                option = {"label": option}
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or option.get("display_label") or "").strip()
            if not label:
                continue
            option_id = str(option.get("id") or option.get("option_id") or "co-" + hashlib.sha256(f"{question['id']}:{index}:{label}".encode()).hexdigest()[:24])
            bundle_options.append({**option, "option_id": option_id, "display_label": label})
        question["options"] = bundle_options
        plan["question"] = question
        if bundle_options:
            plan["option_bundle"] = {"contract_version": "clarification-option-bundle-v2", "question_id": question["id"], "role": question["role"], "options": bundle_options}
        cp = create_checkpoint(
            workdir=workdir,
            article_body_sha256=article_body_sha256,
            title=supplied_title,
            article_id=article_id,
            clarification_history=clarification_history,
            runtime_fingerprint=runtime_fingerprint,
            config_sha256=config_sha256,
            resume_from_stage=resume_from,
            clarification_plan=plan,
            option_bundle=plan.get("option_bundle"),
            speculative_bundle=plan.get("speculative_bundle"),
            binding_continuation=plan.get("binding_continuation"),
            changed_roles=list(pending.get("changed_roles") or []),
            invalidated_stages=list(pending.get("invalidated_stages") or []),
            reusable_artifacts=list(pending.get("reusable_artifacts") or ["l1", "l2", "layers"]),
        )
        if cp.root != workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        return _with_checkpoint(
            _clarification_response({**question, "plan_sha256": _receipt_sha256(plan), "speculative": plan.get("speculative")}, str(plan["reason"])),
            token=cp.token,
            resume_from_stage=resume_from,
        )

    try:
        # L1(결정론적·HCX 없음)은 최초 실행에서만 수행한다.  Resume은
        # 봉인된 01/02 산출물의 존재와 cardinality를 그대로 사용한다.
        if not resume_token:
            run_trace(articles_path=articles_path, output_root=out_root, stage="l1")
        if _value_claim_count() == 0:
            # 수치 주장이 없으면 기사가 아니다(질문·잡담). 상위 라우터가 처리하도록 신호만 준다.
            return {"type": "not_article", "reason": "no_numeric_claims"}
        # Checkpoint continuation starts only at the stage recorded when the
        # question was issued.  L1/L2 artifacts are immutable and never rerun.
        if resume_token:
            if resume_from_stage not in {"layers", "retrieval", "binding", "live"}:
                raise BackendError("RESUME_CHECKPOINT_INVALID", "검증 재개 단계가 올바르지 않습니다.", status_code=409)
            prepare_resume = getattr(run_trace, "_prepare_resume", None)
            if prepare_resume is None:
                raise BackendError("RESUME_CHECKPOINT_UNSUPPORTED", "현재 pipeline runtime은 재개를 지원하지 않습니다.", status_code=409)
            prepare_resume(out_root, resume_from_stage)
            # The trace has one physical live envelope.  A binding checkpoint
            # supplies its sealed continuation bundle to that envelope, where
            # retrieval/reranking/profile transport calls are bypassed.
            remaining = ["layers"] if resume_from_stage == "layers" else []
            if live:
                remaining.append("live")
        else:
            # 수치 주장이 있으면 나머지 단계를 진행한다(인프라 있으면 live까지).
            remaining = ["l2", "layers"] + (["live"] if live else [])
        for stage in remaining:
            if stage == "live" and live and os.getenv("PIPELINE_EARLY_CLARIFICATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
                pending = _pre_live_clarification_plan(out_root, body=body, article_date=article_date)
                if pending is not None:
                    # Indicator/item are the only high-impact Gate-A roles.
                    # Use the release-bound runtime planning path before asking;
                    # failures are bounded to the safe free-text question.
                    if pending.get("speculative"):
                        planner = getattr(run_trace, "_run_speculative", None)
                        if callable(planner):
                            try:
                                speculative_root = workdir / ".speculative_runtime"
                                planned = planner(
                                    _pipeline_config_path(), articles_path, speculative_root,
                                    include_technical_canary=False,
                                    precomputed_l2_manifest_path=out_root / "02_manifest.json",
                                    precomputed_routed_manifest_path=out_root / "03_manifest.json",
                                    role_aware_dimension_shadow=True,
                                    planning_only=True,
                                )
                                runtime_plan = planned.get("clarification_plan") if isinstance(planned, Mapping) else None
                                if isinstance(runtime_plan, Mapping):
                                    pending = dict(runtime_plan)
                                    pending["speculative"] = True
                                    pending["speculative_bundle"] = runtime_plan.get("speculative_bundle")
                                    pending["speculative_audit"] = planned.get("speculative_retrieval")
                            except Exception:
                                # Planning has no authority to turn a missing
                                # field into a 5xx.  The checkpoint records the
                                # bounded fallback and user input triggers full
                                # retrieval on resume.
                                pending = {**pending, "speculative_bundle": {"contract_version": "speculative-bundle-v1", "status": "FREE_TEXT_FALLBACK"}}
                    preserve_workdir = True
                    return {
                        **_make_pending_checkpoint(pending, str(pending.get("resume_from_stage") or "layers")),
                        "title": title, "date": article_date, "date_source": article_date_source,
                        "clarification_history": clarification_history,
                        "timing": {"contract_version": "pipeline-timing-v1", "stages": stage_timings, "resume": {"used": bool(resume_token), "from_stage": resume_from_stage or None}},
                    }
            live_kwargs: dict[str, Any] = {}
            if stage == "live":
                live_kwargs = {
                    # An article is a multi-target request.  Do not select a
                    # representative LEVEL/change claim before operational
                    # execution; explicit single-query routes may still use
                    # the deterministic selector directly.
                    "claim_query": None,
                    "role_aware_dimension_shadow": True,
                }
            if resume_token:
                live_kwargs["clarification_context_path"] = checkpoint.context_path
            started = time.monotonic_ns()
            trace_kwargs: dict[str, Any] = {
                "articles_path": articles_path,
                "output_root": out_root,
                "stage": stage,
                "config_path": _pipeline_config_path() if stage in ("all", "live") else None,
            }
            if stage == "live":
                trace_kwargs["failure_recovery_shadow"] = True
                trace_kwargs.update(live_kwargs)
            run_trace(**trace_kwargs)
            stage_timings[stage] = {"wall_ms": max(0, int(round((time.monotonic_ns() - started) / 1_000_000))), "calls": 1}
        segments = _project_segments(out_root, body, live=live)
        if live:
            live_status, target_receipts = _live_status(out_root)
        if live and len(clarification_history) < 3:
            pending = _pending_article_date_from_live(
                out_root, body=body, article_date=article_date,
            )
            if pending is None:
                pending = _pending_clarification(out_root)
            if pending is None and not article_date:
                pending = _pending_article_date_from_routed(out_root)
            if pending:
                role = pending.get("question", {}).get("role")
                resume_from = (
                    "layers" if role in {"article_date", "period", "indicator"}
                    else "retrieval" if role in {"item", "unit", "source", "population"}
                    else "binding" if role in {"region", "sex", "age", "classification", "measurement_basis"}
                    else "live"
                )
                old_checkpoint = checkpoint
                checkpoint_response = _make_pending_checkpoint(
                    pending,
                    resume_from,
                    plan_override=_pending_clarification_plan(out_root),
                )
                if old_checkpoint is not None:
                    discard_checkpoint(old_checkpoint)
                preserve_workdir = True
                return {
                    **checkpoint_response,
                    "title": title,
                    "date": article_date,
                    "date_source": article_date_source,
                    "clarification_history": clarification_history,
                    "timing": {"contract_version": "pipeline-timing-v1", "stages": stage_timings, "resume": {"used": bool(resume_token), "from_stage": resume_from_stage or None}},
                }
    except BackendError:
        raise
    except trace_stage_error as exc:
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
        if not preserve_workdir:
            if checkpoint is not None:
                discard_checkpoint(checkpoint)
            else:
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
        "status": live_status,
        "live": live,
        "title": title,
        "date": article_date,
        "date_source": article_date_source,
        "clarification_history": clarification_history,
        "summary": counts,
        "target_receipts": target_receipts,
        "timing": {"contract_version": "pipeline-timing-v1", "stages": stage_timings, "resume": {"used": bool(resume_token), "from_stage": resume_from_stage or None}},
        "results": segments,
    }
