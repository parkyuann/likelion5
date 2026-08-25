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

from src.develop.hcx_client import call_hcx_json


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


def deterministic_fallback(packet: EvidencePacket) -> dict[str, Any]:
    p = packet.placeholders
    claim = p.get("CLAIM", "해당 주장").strip().rstrip(".!?。")
    if packet.verdict == "VERIFIED":
        headline = "공식 통계와 일치합니다."
        explanation = f"기사의 ‘{claim}’라는 주장은 KOSIS 공식 셀 값과 허용 오차 안에서 일치합니다."
    elif packet.verdict == "REFUTED":
        headline = "공식 통계와 일치하지 않습니다."
        explanation = f"기사의 ‘{claim}’라는 주장은 KOSIS 공식 셀 값과 일치하지 않습니다."
    else:
        headline = "현재 근거만으로 검증할 수 없습니다."
        limitation = p.get("LIMITATION", "필요한 공식 통계 셀을 유일하게 확정하지 못했습니다.")
        explanation = f"{claim} — {limitation}"
    return {
        "verdict": packet.verdict,
        "headline": headline,
        "explanation": explanation,
        "limitation": p.get("LIMITATION", ""),
        "citation_ids": [document.document_id for document in packet.documents if document.content],
        "answer_contract": ANSWER_CONTRACT,
        "packet_sha256": packet.packet_sha256,
        "fallback": True,
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
    brief: Mapping[str, Any] | None = None
    attempts = 0
    rejection_codes: list[str] = []
    try:
        if use_rag:
            if rag is None:
                raise RuntimeError("RAG_REASONING_UNAVAILABLE")
            brief = rag.synthesize(packet)
            if brief.get("packet_sha256") != packet.packet_sha256:
                raise ValueError("RAG_PACKET_DRIFT")
            if any(citation not in EVIDENCE_IDS for citation in brief.get("citation_ids") or []):
                raise ValueError("RAG_INVALID_CITATION")
        if hcx is None:
            raise RuntimeError("HCX_UNAVAILABLE")
        attempts += 1
        draft = hcx.render(packet, brief)
        try:
            result = validate_and_render_answer(packet, draft)
            return {
                **result,
                "answer_attempts": attempts,
                "repair_attempted": False,
                "rejection_codes": [],
            }
        except (ValueError, KeyError, TypeError) as exc:
            rejection_codes.append(str(exc).split(":", 1)[0] or type(exc).__name__)
        attempts += 1
        repaired = hcx.render(packet, brief, rejection_codes[-1])
        try:
            result = validate_and_render_answer(packet, repaired)
            return {
                **result,
                "answer_attempts": attempts,
                "repair_attempted": True,
                "rejection_codes": rejection_codes,
            }
        except (ValueError, KeyError, TypeError) as exc:
            rejection_codes.append(str(exc).split(":", 1)[0] or type(exc).__name__)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        rejection_codes.append(str(exc).split(":", 1)[0] or type(exc).__name__)
    return {
        **deterministic_fallback(packet),
        "answer_attempts": attempts,
        "repair_attempted": attempts > 1,
        "rejection_codes": rejection_codes,
    }


__all__ = [
    "ANSWER_CONTRACT", "EvidenceDocument", "EvidencePacket", "Hcx007AnswerClient",
    "NcpRagReasoningClient", "build_evidence_packet", "deterministic_fallback",
    "generate_guarded_answer", "validate_and_render_answer",
]
