"""Sealed evidence synthesis and guarded HCX-007 answer rendering.

RAG Reasoning and HCX are presentation layers only.  Neither can mutate the
deterministic verdict, selector plan, canonical quantities, or candidate set.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence
import uuid

import requests

from src.news_verification.runtime.hcx_client import call_hcx_json


ANSWER_CONTRACT = "operational-answer-v2"
EVIDENCE_IDS = ("claim-source", "binding-plan", "official-cell", "comparison", "limitation")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_DIGIT = re.compile(r"\d")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


@dataclass(frozen=True)
class EvidenceDocument:
    document_id: str
    content: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class EvidencePacket:
    verdict: str
    documents: tuple[EvidenceDocument, ...]
    placeholders: Mapping[str, str]
    packet_sha256: str


class RagReasoner(Protocol):
    def synthesize(self, packet: EvidencePacket) -> Mapping[str, Any]: ...


class HcxAnswerer(Protocol):
    def render(
        self,
        packet: EvidencePacket,
        brief: Mapping[str, Any] | None,
        repair_code: str | None = None,
    ) -> Mapping[str, Any]: ...


class _AcceptanceError(ValueError):
    """A post-validator integrity failure, not a validator rejection code."""


def build_evidence_packet(
    *,
    verdict: str,
    claim_source: Mapping[str, Any],
    binding_plan: Mapping[str, Any],
    official_cell: Mapping[str, Any],
    comparison: Mapping[str, Any],
    limitation: Mapping[str, Any],
    placeholders: Mapping[str, Any],
) -> EvidencePacket:
    if verdict not in {"VERIFIED", "REFUTED", "UNVERIFIABLE"}:
        raise ValueError("unsupported deterministic verdict")
    payloads = (claim_source, binding_plan, official_cell, comparison, limitation)
    documents = tuple(
        EvidenceDocument(doc_id, dict(payload), hashlib.sha256(_canonical(payload)).hexdigest())
        for doc_id, payload in zip(EVIDENCE_IDS, payloads, strict=True)
    )
    replacement = {str(key): str(value) for key, value in placeholders.items()}
    packet_payload = {
        "verdict": verdict,
        "documents": [{"id": doc.document_id, "sha256": doc.sha256} for doc in documents],
        "placeholders": replacement,
    }
    return EvidencePacket(verdict, documents, replacement, hashlib.sha256(_canonical(packet_payload)).hexdigest())


class NcpRagReasoningClient:
    """Two-step RAG client whose single tool can return sealed documents only."""

    endpoint = "https://clovastudio.stream.ntruss.com/v1/api-tools/rag-reasoning"

    def __init__(self, api_key: str, *, timeout_seconds: float = 120.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _post(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.api_key:
            raise RuntimeError("RAG_REASONING_UNAVAILABLE")
        response = requests.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"RAG_REASONING_UNAVAILABLE:{response.status_code}")
        return response.json()

    def synthesize(self, packet: EvidencePacket) -> Mapping[str, Any]:
        tool = {
            "type": "function",
            "function": {
                "name": "verification_evidence_lookup",
                "description": "Return only the already sealed verification evidence documents. Never search or change a verdict.",
                "parameters": {
                    "type": "object",
                    "properties": {"document_ids": {"type": "array", "items": {"type": "string"}}},
                    "required": ["document_ids"],
                },
            },
        }
        user = (
            f"Deterministic verdict is {packet.verdict}. Summarize only supplied evidence, "
            "cite document IDs, and do not calculate, select candidates, or change the verdict."
        )
        first_body = {
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "toolChoice": {"type": "function", "function": {"name": "verification_evidence_lookup"}},
            "temperature": 0.1,
            "topP": 0.8,
            "topK": 0,
            "maxTokens": 1024,
            "seed": 1,
        }
        first = self._post(first_body)
        assistant = dict(first.get("result", {}).get("message") or {})
        calls = list(assistant.get("toolCalls") or [])
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != "verification_evidence_lookup":
            raise RuntimeError("RAG_REASONING_INVALID_TOOL_CALL")
        call_id = str(calls[0].get("id") or "")
        search_result = [
            {"id": document.document_id, "doc": json.dumps(document.content, ensure_ascii=False, sort_keys=True)}
            for document in packet.documents
        ]
        second_body = {
            "messages": [
                {"role": "user", "content": user},
                {"role": "assistant", "content": "", "toolCalls": calls},
                {"role": "tool", "toolCallId": call_id, "content": json.dumps({"search_result": search_result}, ensure_ascii=False)},
            ],
            "tools": [tool],
            "toolChoice": "none",
            "temperature": 0.1,
            "topP": 0.8,
            "topK": 0,
            "maxTokens": 1024,
            "seed": 1,
        }
        second = self._post(second_body)
        content = str(second.get("result", {}).get("message", {}).get("content") or "").strip()
        citations = sorted({doc_id for doc_id in EVIDENCE_IDS if doc_id in content})
        return {
            "summary": content,
            "citation_ids": citations,
            "shadow_suggested_queries": [],
            "packet_sha256": packet.packet_sha256,
        }


class Hcx007AnswerClient:
    """HCX-007 structured-output verbalizer restricted to placeholders."""

    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["VERIFIED", "REFUTED", "UNVERIFIABLE"]},
            "headline": {"type": "string"},
            "explanation": {"type": "string"},
            "limitation": {"type": "string"},
            "citation_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "headline", "explanation", "limitation", "citation_ids"],
    }

    def __init__(self, api_key: str, *, timeout_seconds: int = 120) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def render(
        self,
        packet: EvidencePacket,
        brief: Mapping[str, Any] | None,
        repair_code: str | None = None,
    ) -> Mapping[str, Any]:
        prompt = {
            "verdict": packet.verdict,
            "allowed_placeholders": sorted(packet.placeholders),
            "allowed_citation_ids": [doc.document_id for doc in packet.documents],
            "evidence_brief": dict(brief or {}),
            "rule": "Do not write digits or factual names directly. Use only {{PLACEHOLDER}} tokens for sealed facts.",
        }
        if repair_code:
            prompt["repair"] = {
                "rejection_code": repair_code,
                "instruction": "Regenerate once from the same sealed packet. Do not infer any new fact.",
            }
        result, _, _ = call_hcx_json(
            system_prompt="You verbalize a sealed deterministic fact-check result without changing any fact.",
            user_prompt=json.dumps(prompt, ensure_ascii=False, sort_keys=True),
            schema=self.schema,
            api_key=self.api_key,
            model="HCX-007",
            timeout=self.timeout_seconds,
            temperature=0.0,
            top_p=0.8,
            seed=1,
            max_completion_tokens=800,
        )
        return result


def validate_and_render_answer(packet: EvidencePacket, draft: Mapping[str, Any]) -> dict[str, Any]:
    """Validate model output and substitute only sealed facts."""
    if str(draft.get("verdict") or "") != packet.verdict:
        raise ValueError("ANSWER_VERDICT_DRIFT")
    allowed_ids = {document.document_id for document in packet.documents}
    citations = [str(value) for value in draft.get("citation_ids") or []]
    if not citations or any(citation not in allowed_ids for citation in citations):
        raise ValueError("ANSWER_INVALID_CITATION")
    result = {"verdict": packet.verdict, "citation_ids": citations}
    for field_name in ("headline", "explanation", "limitation"):
        value = str(draft.get(field_name) or "")
        if _DIGIT.search(value):
            raise ValueError("ANSWER_UNSEALED_NUMBER")
        placeholders = _PLACEHOLDER.findall(value)
        if any(name not in packet.placeholders for name in placeholders):
            raise ValueError("ANSWER_UNKNOWN_PLACEHOLDER")
        result[field_name] = _PLACEHOLDER.sub(lambda match: packet.placeholders[match.group(1)], value)
    if packet.verdict == "UNVERIFIABLE" and any(token in result["explanation"] for token in ("공식값은", "공식 수치는")):
        raise ValueError("ANSWER_UNVERIFIABLE_ASSERTION")
    result["answer_contract"] = ANSWER_CONTRACT
    result["packet_sha256"] = packet.packet_sha256
    return result


def _packet_integrity_error(packet: EvidencePacket) -> str | None:
    expected_documents: list[dict[str, str]] = []
    if len(packet.documents) != len(EVIDENCE_IDS):
        return "ANSWER_PACKET_DOCUMENT_COUNT"
    if tuple(document.document_id for document in packet.documents) != EVIDENCE_IDS:
        return "ANSWER_PACKET_DOCUMENT_ORDER"
    seen_ids: set[str] = set()
    for document in packet.documents:
        if document.document_id in seen_ids or document.document_id not in EVIDENCE_IDS:
            return "ANSWER_PACKET_DOCUMENT_ID"
        seen_ids.add(document.document_id)
        actual_sha256 = hashlib.sha256(_canonical(document.content)).hexdigest()
        if actual_sha256 != document.sha256:
            return "ANSWER_DOCUMENT_HASH_MISMATCH"
        expected_documents.append({"id": document.document_id, "sha256": actual_sha256})
    packet_payload = {
        "verdict": packet.verdict,
        "documents": expected_documents,
        "placeholders": {str(key): str(value) for key, value in packet.placeholders.items()},
    }
    expected_packet_sha256 = hashlib.sha256(_canonical(packet_payload)).hexdigest()
    if expected_packet_sha256 != packet.packet_sha256:
        return "ANSWER_PACKET_HASH_MISMATCH"
    return None


def _content_bearing_document_ids(packet: EvidencePacket) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for document_id in EVIDENCE_IDS:
        for document in packet.documents:
            if document.document_id != document_id or document_id in seen:
                continue
            content = document.content
            content_bearing = bool(content)
            if document_id == "official-cell" and "cell" in content:
                cell = content.get("cell")
                content_bearing = bool(cell) if isinstance(cell, Mapping) else bool(str(cell or "").strip())
            if content_bearing:
                result.append(document_id)
                seen.add(document_id)
    return result


def _validated_acceptance(packet: EvidencePacket, draft: Mapping[str, Any]) -> dict[str, Any]:
    """Run the frozen validator, then independently re-check its acceptance boundary."""
    result = validate_and_render_answer(packet, draft)
    packet_error = _packet_integrity_error(packet)
    if packet_error:
        raise _AcceptanceError(packet_error)
    citations = [str(value) for value in result.get("citation_ids") or []]
    content_ids = _content_bearing_document_ids(packet)
    if not citations or len(citations) != len(set(citations)):
        raise _AcceptanceError("ANSWER_INVALID_CITATION")
    if not set(citations).issubset(set(content_ids)):
        raise _AcceptanceError("ANSWER_INVALID_CITATION")
    return {
        **result,
        "schema_valid": True,
        "evidence_packet_valid": True,
        "citation_valid": True,
        "accepted": True,
    }


def _deterministic_draft(packet: EvidencePacket) -> dict[str, Any]:
    """Build only fixed literals and placeholder tokens; never interpolate facts here."""
    placeholders = packet.placeholders
    has_claim = bool("CLAIM" in placeholders and str(placeholders.get("CLAIM") or "").strip())
    has_official_value = bool(
        packet.verdict != "UNVERIFIABLE"
        and "OFFICIAL_VALUE" in placeholders
        and str(placeholders.get("OFFICIAL_VALUE") or "").strip()
    )
    has_limitation = bool("LIMITATION" in placeholders and str(placeholders.get("LIMITATION") or "").strip())
    if packet.verdict == "VERIFIED":
        headline = "현재 확인된 공식 통계값을 설명합니다."
        explanation = "검증 대상 주장: {{CLAIM}}. 현재 확인된 공식 통계값을 설명합니다." if has_claim else headline
    elif packet.verdict == "REFUTED":
        headline = "현재 확인된 공식 통계값을 설명합니다."
        explanation = "검증 대상 주장: {{CLAIM}}. 현재 확인된 공식 통계값을 설명합니다." if has_claim else headline
    else:
        headline = "현재 근거만으로 검증할 수 없습니다."
        explanation = "검증 대상 주장: {{CLAIM}}. 현재 근거만으로 검증할 수 없습니다." if has_claim else headline
    if has_official_value:
        explanation += " 공식값은 {{OFFICIAL_VALUE}}입니다."
    if has_limitation:
        explanation += " 제한: {{LIMITATION}}"
    limitation = "{{LIMITATION}}" if has_limitation else "추가 근거가 필요합니다."
    return {
        "verdict": packet.verdict,
        "headline": headline,
        "explanation": explanation,
        "limitation": limitation,
        "citation_ids": _content_bearing_document_ids(packet),
    }


def deterministic_fallback(
    packet: EvidencePacket,
    *,
    fallback_reason: str = "DETERMINISTIC_FALLBACK_REQUESTED",
    failed_channel: str = "DETERMINISTIC",
    answer_attempts: int = 0,
    repair_attempted: bool = False,
    rejection_codes: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a deterministic answer only after it passes the unchanged validator."""
    try:
        result = _validated_acceptance(packet, _deterministic_draft(packet))
    except Exception as exc:
        raise RuntimeError("ANSWER_DETERMINISTIC_FALLBACK_INVALID") from exc
    return {
        **result,
        "answer_attempts": answer_attempts,
        "repair_attempted": repair_attempted,
        "rejection_codes": list(rejection_codes),
        "fallback": True,
        "fallback_kind": "VALIDATED_DETERMINISTIC",
        "fallback_reason": fallback_reason,
        "failed_channel": failed_channel,
        "renderer": "DETERMINISTIC_SEALED_V1",
    }


def generate_guarded_answer(
    packet: EvidencePacket,
    hcx: HcxAnswerer | None,
    *,
    rag: RagReasoner | None = None,
    use_rag: bool = False,
) -> dict[str, Any]:
    """Use model presentation only when both layers pass strict validation.

    A validator rejection gets exactly one repair request with the same sealed
    packet and only the rejection code.  Service failures fall back immediately.
    """
    packet_error = _packet_integrity_error(packet)
    if packet_error:
        return deterministic_fallback(
            packet, fallback_reason="EVIDENCE_PACKET_INTEGRITY_FAILURE", failed_channel="PACKET",
        )

    brief: Mapping[str, Any] | None = None
    attempts = 0
    rejection_codes: list[str] = []
    if use_rag:
        try:
            if rag is None:
                raise RuntimeError("RAG_REASONING_UNAVAILABLE")
            brief = rag.synthesize(packet)
            if brief.get("packet_sha256") != packet.packet_sha256:
                raise ValueError("RAG_PACKET_DRIFT")
            if any(citation not in EVIDENCE_IDS for citation in brief.get("citation_ids") or []):
                raise ValueError("RAG_INVALID_CITATION")
        except Exception:
            return deterministic_fallback(
                packet, fallback_reason="RAG_SERVICE_FAILURE", failed_channel="RAG",
            )

    if hcx is None:
        return deterministic_fallback(
            packet, fallback_reason="HCX_INITIAL_CALL_FAILURE", failed_channel="HCX_INITIAL",
        )
    attempts += 1
    try:
        draft = hcx.render(packet, brief)
    except Exception:
        return deterministic_fallback(
            packet, fallback_reason="HCX_INITIAL_CALL_FAILURE", failed_channel="HCX_INITIAL",
            answer_attempts=attempts,
        )
    try:
        result = _validated_acceptance(packet, draft)
        return {
            **result,
            "answer_attempts": attempts,
            "repair_attempted": False,
            "rejection_codes": [],
            "fallback": False,
            "fallback_kind": "NONE",
            "fallback_reason": None,
            "failed_channel": None,
            "renderer": "HCX_007",
        }
    except _AcceptanceError:
        return deterministic_fallback(
            packet, fallback_reason="HCX_ACCEPTANCE_INTEGRITY_FAILURE", failed_channel="HCX_ACCEPTANCE",
            answer_attempts=attempts,
        )
    except Exception as exc:
        rejection_codes.append(str(exc).split(":", 1)[0] or type(exc).__name__)

    attempts += 1
    try:
        repaired = hcx.render(packet, brief, rejection_codes[-1])
    except Exception:
        return deterministic_fallback(
            packet, fallback_reason="HCX_REPAIR_CALL_FAILURE", failed_channel="HCX_REPAIR",
            answer_attempts=attempts, repair_attempted=True, rejection_codes=rejection_codes,
        )
    try:
        result = _validated_acceptance(packet, repaired)
        return {
            **result,
            "answer_attempts": attempts,
            "repair_attempted": True,
            "rejection_codes": rejection_codes,
            "fallback": False,
            "fallback_kind": "NONE",
            "fallback_reason": None,
            "failed_channel": None,
            "renderer": "HCX_007",
        }
    except _AcceptanceError:
        return deterministic_fallback(
            packet, fallback_reason="HCX_ACCEPTANCE_INTEGRITY_FAILURE", failed_channel="HCX_ACCEPTANCE",
            answer_attempts=attempts, repair_attempted=True, rejection_codes=rejection_codes,
        )
    except Exception as exc:
        rejection_codes.append(str(exc).split(":", 1)[0] or type(exc).__name__)
    return deterministic_fallback(
        packet, fallback_reason="HCX_VALIDATOR_REJECTED_TWICE", failed_channel="HCX_VALIDATOR",
        answer_attempts=attempts, repair_attempted=True, rejection_codes=rejection_codes,
    )


__all__ = [
    "ANSWER_CONTRACT", "EvidenceDocument", "EvidencePacket", "Hcx007AnswerClient",
    "NcpRagReasoningClient", "build_evidence_packet", "deterministic_fallback",
    "generate_guarded_answer", "validate_and_render_answer",
]



