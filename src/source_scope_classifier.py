"""Deterministic source-scope classification from an extracted organization name.

The LLM is responsible only for copying ``source_org_raw`` from the input text.
KOSIS membership is decided locally against ``data/kosis_org_names.json`` so the
pipeline cannot invent a KOSIS organization or an out-of-schema scope label.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


SOURCE_SCOPES = ("KOSIS등재", "공식기관_비KOSIS", "민간기관", "해외기관", "불명")
SCOPE_METHOD = "kosis_org_dictionary_v1"

# Only verified renames and common abbreviations of catalog organizations belong
# here.  The value must be the exact current name in kosis_org_names.json.
KOSIS_ORG_ALIASES = {
    "통계청": "국가데이터처",
    "국가데이터": "국가데이터처",
    "한은": "한국은행",
    "공정위": "공정거래위원회",
    "기재부": "기획예산처",
    "국토부": "국토교통부",
    "산업부": "산업통상부",
    "산업통상자원부": "산업통상부",
    "복지부": "보건복지부",
    "고용부": "고용노동부",
    "노동부": "고용노동부",
    "행안부": "행정안전부",
    "금융위": "금융위원회",
    "농식품부": "농림축산식품부",
    "해수부": "해양수산부",
    "중기부": "중소벤처기업부",
    "금융투자협회": "한국금융투자협회",
    "전력거래소": "한국전력거래소",
    "오피넷": "한국석유공사",
    "aT": "한국농수산식품유통공사",
}

UNKNOWN_OR_NOISE = {
    "", "불명", "없음", "미상", "null", "none", "nan",
    "보고서", "자료", "통계", "결과", "조사", "발표", "보도자료",
    "관련 기관", "관련 기관 보도자료", "관련 통계자료", "알려짐",
}

FOREIGN_RE = re.compile(
    r"(?:미국|미 정부|미 노동부|영국|중국|일본|독일|프랑스|캐나다|러시아|"
    r"유럽연합|EU|연준|FOMC|IMF|OECD|UN|세계은행|국제통화기금|"
    r"국제에너지기구|국제원자력기구|IAEA|해외|국제기구)",
    re.IGNORECASE,
)

DOMESTIC_PUBLIC_RE = re.compile(
    r"(?:정부|위원회|부처|공단|공사|청|부|원|지자체|시청|도청|구청|군청|"
    r"국책연구기관|연구원|대학교|대학|법원|국회)$"
)


@dataclass(frozen=True)
class ScopeDecision:
    scope: str
    matched_org_id: str = ""
    matched_org_name: str = ""
    method: str = SCOPE_METHOD
    reason: str = ""


def normalize_org_name(value: object) -> str:
    """Normalize harmless typography without performing fuzzy matching."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text.casefold() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"[\s·ㆍ,.'\"`]+", "", text)


def load_kosis_org_catalog(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"KOSIS organization catalog must be a JSON object: {path}")
    catalog = {str(org_id): str(name).strip() for org_id, name in payload.items() if str(name).strip()}
    if not catalog:
        raise ValueError(f"KOSIS organization catalog is empty: {path}")
    return catalog


def _catalog_index(catalog: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    return {
        normalize_org_name(name): (str(org_id), str(name).strip())
        for org_id, name in catalog.items()
        if normalize_org_name(name)
    }


def classify_source_scope(org_raw: object, catalog: Mapping[str, str]) -> ScopeDecision:
    """Classify a cached/extracted organization without any external API call.

    KOSIS membership uses exact normalized catalog or verified-alias matching only.
    This intentionally favors precision: an unresolved name is never promoted to
    ``KOSIS등재`` by a fuzzy or substring match.
    """
    raw = "" if org_raw is None else str(org_raw).strip()
    normalized = normalize_org_name(raw)
    if not normalized or raw.casefold() in UNKNOWN_OR_NOISE or normalized.casefold() in UNKNOWN_OR_NOISE:
        return ScopeDecision("불명", reason="기관명 없음 또는 추출 노이즈")

    index = _catalog_index(catalog)
    matched = index.get(normalized)
    if matched:
        return ScopeDecision("KOSIS등재", matched[0], matched[1], reason="181개 KOSIS 기관 사전 정확 일치")

    alias_target = KOSIS_ORG_ALIASES.get(raw) or KOSIS_ORG_ALIASES.get(normalized)
    if alias_target:
        matched = index.get(normalize_org_name(alias_target))
        if matched:
            return ScopeDecision("KOSIS등재", matched[0], matched[1], reason=f"검증된 기관 별칭 일치: {raw}")

    if FOREIGN_RE.search(raw):
        return ScopeDecision("해외기관", reason="해외 정부·국제기구 표지어 일치")
    if DOMESTIC_PUBLIC_RE.search(raw):
        return ScopeDecision("공식기관_비KOSIS", reason="국내 공공기관 형식이나 KOSIS 사전 미등재")
    return ScopeDecision("민간기관", reason="기관명은 있으나 KOSIS·공공·해외 규칙에 미일치")
