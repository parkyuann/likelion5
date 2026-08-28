"""Central registry and provenance controls for HCX lexical heuristics.

The registry distinguishes source behavior from provenance. A rule is disabled
only when its provenance is explicitly one of the contaminated development
articles; ``UNKNOWN`` rules remain enabled.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEV6_ARTICLE_IDS = frozenset({"380", "1203", "2568", "2615", "2677", "2703"})
DISABLE_DEV6_ENV = "HCX_DISABLE_DEV6_LEXICAL_RULES"
UNKNOWN_PROVENANCE = "UNKNOWN"

# Shared lexical extractors and gates. These were previously interleaved with
# orchestration code in article_claim_pipeline.py. Their historical trigger
# article is unknown unless a rule is listed in ``LEXICAL_RULES`` below.
_AGE_RANGE_RE = re.compile(r"\d{1,3}\s*[~\-]\s*\d{1,3}\s*세")
_AGE_INDICATOR_VALUE_RE = re.compile(
    r"(?:\d{1,3}\s*[~\-]\s*\d{1,3}\s*세|"
    r"\d{1,3}\s*세\s*(?:이상|이하|미만)|\d{1,3}\s*대)"
)
_INDEX_LEVEL_VALUE_RE = re.compile(
    r"(?:지수|순환변동치)\s*(?:는|은|이|가)?\s*"
    r"(?P<value>[-−]?\d[\d,]*(?:\.\d+)?)\s*(?=(?:로|이며|이었다|였다|이다))"
)
_CONSECUTIVE_DURATION_VALUE_RE = re.compile(
    r"(?P<value>\d[\d,]*)\s*(?P<unit>개월)\s*연속"
)
_RELATIVE_PERIOD_RANGE_VALUE_RE = re.compile(
    r"(?P<value>(?:지난해|작년|올해|금년)\s*\d{1,2}분기)"
    r"\s*(?:[~∼\-]|부터)\s*"
    r"(?P<end>(?:지난해|작년|올해|금년)\s*\d{1,2}분기)"
    r"(?:까지)?"
)
_PERIOD_CANDIDATE_RE = re.compile(
    r"(?:지난해|작년|올해|금년|이달|지난달|이번\s?달|전년\s?동월|"
    r"전년|전월|전분기|동월|동기)"
    r"(?:\s*\d{1,2}(?:월|분기))?(?:\s*(?:초|중|말))?|"
    r"\d{4}년(?:\s*\d{1,2}(?:월|분기))?(?:\s*(?:초|중|말))?|\d{1,2}분기"
)
_RELATIVE_MEASUREMENT_PERIOD_RE = re.compile(
    r"(?:지난해|작년|올해|금년|이달|지난달|이번\s?달)"
    r"(?:\s*\d{1,2}(?:월|분기))?(?:\s*(?:초|중|말))?"
)
_INDICATOR_SOURCE_ORG_RE = re.compile(
    r"(?:통계청|국가데이터처|한국은행|한은|금융감독원|금감원|행정안전부|"
    r"보건복지부|교육부|국토교통부|고용노동부|산업통상자원부|"
    r"농림축산식품부|해양수산부|질병관리청|에\s*따르면|"
    r"가\s*발표한|이\s*발표한)"
)
_INDICATOR_CLAUSE_END_RE = re.compile(
    r"(?:기록(?:했다|됐다|함)|집계(?:됐다|함)|발표(?:했다|함)|"
    r"증가(?:했다|함)|감소(?:했다|함)|상승(?:했다|함)|하락(?:했다|함)|"
    r"늘었(?:다|음)|늘어남|줄었(?:다|음)|줄어듦|올랐(?:다|음)|"
    r"내렸(?:다|음)|높아졌(?:다|음)|낮아졌(?:다|음)|"
    r"나타났(?:다|음)|보였(?:다|음)|차지(?:했다|함)|기록함)[.!?]?$"
)
_REGION_NAMES = (
    "대구경북권", "동남권", "수도권", "충청권", "대경권", "전국", "서울",
    "부산", "대구", "인천", "광주", "대전", "울산", "세종", "제주", "경기",
    "강원", "충북", "충남", "전북", "전남", "경북", "경남",
)
_REGION_STOPWORDS = frozenset({
    "따르면", "보다도", "역시", "내구", "자동", "반도", "수도", "기여도",
    "생산도", "소비자물가동", "산업활동동", "오르면", "늘어나면",
    "경제협력개발기구",
})
_INDUSTRY_STOPWORDS = frozenset({
    "개인사업", "자영업", "사업", "산업", "공업제품", "기여도",
})
_REGION_PATTERN = re.compile(
    r"(?:[가-힣]{2,8}(?:특별자치시|특별자치도|특별시|광역시|자치도|"
    r"시|군|구|읍|면|동)|"
    + "|".join(map(re.escape, _REGION_NAMES)) + r")"
)
_SPAN_DIMENSION_PATTERNS = {
    "지역": _REGION_PATTERN,
    "성별": re.compile(r"(?:남성|여성|남자|여자|전체)"),
    "연령": re.compile(
        r"(?:\d{1,3}\s*[~\-]\s*\d{1,3}\s*세|"
        r"\d{1,3}\s*세\s*이상|\d{1,3}\s*대|청년층|고령층|노년층)"
    ),
    "산업": re.compile(
        r"(?:운수·창고|금융·보험|광업|제조업|서비스업|건설업|"
        r"도·소매업|숙박·음식점업|"
        r"[가-힣A-Za-z0-9·/]{2,24}(?:산업|업종|업))"
    ),
}
_SEMANTIC_EVIDENCE_TOKEN_RE = re.compile(
    r"[가-힣A-Za-z][가-힣A-Za-z0-9·/]*"
    r"(?:\([가-힣A-Za-z一-龥]{1,12}\)[가-힣A-Za-z0-9·/]*)?"
)
_SEMANTIC_EVIDENCE_PARTICLES = (
    "에게서는", "으로부터", "에서는", "에게서", "으로는", "까지는", "부터는",
    "에서", "으로", "에게", "까지", "부터", "보다", "처럼", "만큼", "의", "은",
    "는", "이", "가", "을", "를", "와", "과", "도", "만", "에",
)
_SEMANTIC_EVIDENCE_STOPWORDS = frozenset({
    "따르면", "발표한", "자료", "기록했다", "집계됐다", "나타났다", "보였다",
    "가장", "수준", "이후", "이래", "현재", "이번", "지난", "지난해", "작년",
    "올해", "금년", "전월", "전년", "동월", "동기", "대비", "경우", "관련",
    "등", "및", "또", "역시", "반면", "다만", "특히", "전반적인", "것으로",
})
_LOCAL_INDICATOR_METRIC_RE = re.compile(
    r"(?:연체율|고용률|성장률|상승률|하락률|증감률|증가율|감소율|변화율|"
    r"생산지수|물가지수|생활물가지수|근원물가|수주액|취업자\s*수|"
    r"생산|판매|투자|가격|비율|규모|금액)"
)
_LOCAL_INDICATOR_LEADING_STOPWORDS = frozenset({
    "다만", "한편", "그러나", "또한", "지난달", "지난해", "작년", "올해",
    "금년", "전년", "전월", "동월", "동기", "대비",
})
_SEMANTIC_DIMENSION_SURFACE_RE = re.compile(
    r"(?:저축은행|시중은행|지방은행|인터넷전문은행|여신전문금융사|"
    r"생명보험사|손해보험사|보험사|증권사|카드사|캐피탈사?|"
    r"전\(全\)산업|전산업)"
)
_POPULATION_SURFACE_RE = re.compile(
    r"(?:개인사업자|자영업자|근로자|노동자|가구|가구원|인구|주민|학생|"
    r"환자|사업체|기업|농가|어가|취업자|실업자|청년|고령자|노인)(?:들)?"
)
_METRIC_ANCHOR_SUFFIXES = (
    "지수", "물가", "생산", "판매", "투자", "수주액", "고용률", "취업자수",
    "연체율", "비율", "금액", "규모", "가격", "증감률", "상승률", "성장률",
)
_ITEM_ANCHOR_STOPWORDS = frozenset({
    "실질", "명목", "전체", "전국", "기준", "OECD", "경제협력개발기구",
})
_COMPARISON_PAREN_PREFIX_RE = re.compile(
    r"(?:\d{4}년(?:\s*\d{1,2}(?:월|분기))?|\d+년\s*전(?:인)?|"
    r"직전\s*달|지난해|작년)[^()]{0,24}\(\s*$"
)
_COMPARISON_PERIOD_RE = re.compile(
    r"(?:전월|전년(?:\s*동월)?|동월|동기|전분기)"
)
_LOCAL_CHANGE_PREDICATE_RE = re.compile(
    r"(?:증가|감소|상승|하락|늘|줄|올랐|내렸|뛰었|떨어)"
)
_ABSOLUTE_PERIOD_RE = re.compile(
    r"\d{4}년(?:\s*\d{1,2}(?:월|분기))?(?:\s*(?:초|중|말))?"
)
_BASELINE_PAIR_RE = re.compile(
    r"(?P<first>[+-]?\d(?:[\d,.\s]|만|억)*(?:명|가구|건|원|세|%))"
    r"\s*에서\s*"
    r"(?P<second>[+-]?\d(?:[\d,.\s]|만|억)*(?:명|가구|건|원|세|%))"
)


@dataclass(frozen=True)
class LexicalRule:
    rule_id: str
    pattern: str
    provenance_article_idx: tuple[str, ...] | str

    def enabled(self, *, disable_dev6: bool | None = None) -> bool:
        disabled = dev6_rules_disabled() if disable_dev6 is None else disable_dev6
        if not disabled or self.provenance_article_idx == UNKNOWN_PROVENANCE:
            return True
        return not bool(
            set(self.provenance_article_idx) & DEV6_ARTICLE_IDS
        )


LEXICAL_RULES = {
    "category_definition_range": LexicalRule(
        "category_definition_range",
        r"\s*(?:미만|이하|이상)(?:인|으?로\s*일(?:한|하(?:는)?))",
        ("380",),
    ),
    "category_definition_population": LexicalRule(
        "category_definition_population",
        r"(?:근로자|집단|대상|계층|연령)",
        ("380",),
    ),
    "category_definition_percent_threshold": LexicalRule(
        "category_definition_percent_threshold",
        r"\s*이상(?:이었|였|이었다|이었다고|으로)",
        ("2615",),
    ),
    "category_definition_rank_prefix": LexicalRule(
        "category_definition_rank_prefix",
        r"(?:그\s*다음|상위|하위)\s*$",
        ("2703",),
    ),
    "category_definition_company_suffix": LexicalRule(
        "category_definition_company_suffix",
        r"\s*(?:개\s*)?(?:기업|회사|사)",
        ("2703",),
    ),
    "policy_or_rule_value": LexicalRule(
        "policy_or_rule_value",
        r"(?:연간\s*배정\s*인원|별도로\s*배정|도입\s*허용\s*비율|"
        r"허용\s*상한|한시적으로\s*확대)",
        ("1203",),
    ),
    "local_out_of_scope_value": LexicalRule(
        "local_out_of_scope_value",
        r"(?:한국경영자총협회[\s\S]{0,80}조사에\s*따르면|"
        r"(?:기업\s*데이터\s*연구소\s*)?CEO스코어[\s\S]{0,120}"
        r"(?:조사|분석)(?:한)?\s*(?:결과|자료)?|"
        r"노인\s*일자리\s*사업[\s\S]{0,80}공익형|"
        r"국가데이터처가\s*추정한[\s\S]{0,80}(?:올해|금년)"
        r"[\s\S]{0,40}자살\s*사망자)",
        ("380", "2568", "2677"),
    ),
    "private_source_context": LexicalRule(
        "private_source_context",
        r"(?:한국경영자총협회|CEO스코어|민간\s*(?:연구소|기관)|"
        r"기업\s*데이터\s*연구소)",
        ("2568",),
    ),
    "policy_target_indicator": LexicalRule(
        "policy_target_indicator",
        r"(?:정책\s*)?목표",
        ("2677",),
    ),
}


def dev6_rules_disabled() -> bool:
    return os.getenv(DISABLE_DEV6_ENV, "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def compiled_rule(
    rule_id: str,
    *,
    disable_dev6: bool | None = None,
) -> re.Pattern[str] | None:
    rule = LEXICAL_RULES[rule_id]
    if not rule.enabled(disable_dev6=disable_dev6):
        return None
    return re.compile(rule.pattern)


def rule_search(
    rule_id: str,
    text: str,
    *,
    disable_dev6: bool | None = None,
) -> re.Match[str] | None:
    pattern = compiled_rule(rule_id, disable_dev6=disable_dev6)
    return pattern.search(text) if pattern else None


def rule_match(
    rule_id: str,
    text: str,
    *,
    disable_dev6: bool | None = None,
) -> re.Match[str] | None:
    pattern = compiled_rule(rule_id, disable_dev6=disable_dev6)
    return pattern.match(text) if pattern else None


_KOREAN_SURFACE_RE = re.compile(
    r"[가-힣]+(?:[ ·/()一-龥A-Za-z0-9%~\-]*[가-힣]+)*"
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    result: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            result.add(id(body[0].value))
    return result


def executable_korean_literal_inventory(
    source_path: Path,
) -> list[dict[str, Any]]:
    """Inventory unique Korean surfaces in executable string constants.

    Docstrings are excluded. Prompt strings remain included because they affect
    runtime model behavior. Provenance is explicit only for centralized rules;
    every other surface is marked ``UNKNOWN`` instead of being guessed from
    mere occurrence in an article.
    """
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    occurrences: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
            or id(node) in docstrings
            or not re.search(r"[가-힣]", node.value)
        ):
            continue
        for match in _KOREAN_SURFACE_RE.finditer(node.value):
            surface = match.group().strip()
            if surface:
                occurrences.setdefault(surface, []).append(
                    int(getattr(node, "lineno", -1))
                )

    known: dict[str, set[str]] = {}
    for rule in LEXICAL_RULES.values():
        if rule.provenance_article_idx == UNKNOWN_PROVENANCE:
            continue
        for match in _KOREAN_SURFACE_RE.finditer(rule.pattern):
            surface = match.group().strip()
            if surface:
                known.setdefault(surface, set()).update(
                    rule.provenance_article_idx
                )
    return [
        {
            "surface": surface,
            "source_files": [str(source_path)],
            "source_lines": sorted(set(lines)),
            "provenance_article_idx": (
                sorted(known[surface])
                if surface in known else UNKNOWN_PROVENANCE
            ),
            "dev6_specific": bool(
                surface in known and known[surface] & DEV6_ARTICLE_IDS
            ),
        }
        for surface, lines in sorted(occurrences.items())
    ]


def merged_executable_korean_literal_inventory(
    source_paths: list[Path],
) -> list[dict[str, Any]]:
    """Merge the executable-surface inventory across the pipeline and registry."""
    merged: dict[str, dict[str, Any]] = {}
    for source_path in source_paths:
        for row in executable_korean_literal_inventory(source_path):
            surface = row["surface"]
            target = merged.setdefault(
                surface,
                {
                    "surface": surface,
                    "source_files": [],
                    "source_locations": [],
                    "provenance_article_idx": UNKNOWN_PROVENANCE,
                    "dev6_specific": False,
                },
            )
            target["source_files"].extend(row["source_files"])
            target["source_locations"].extend(
                {
                    "source_file": str(source_path),
                    "line": line,
                }
                for line in row["source_lines"]
            )
            provenance = row["provenance_article_idx"]
            if provenance != UNKNOWN_PROVENANCE:
                current = target["provenance_article_idx"]
                current_values = set(
                    current if current != UNKNOWN_PROVENANCE else []
                )
                target["provenance_article_idx"] = sorted(
                    current_values | set(provenance)
                )
            target["dev6_specific"] = (
                target["dev6_specific"] or row["dev6_specific"]
            )
    for row in merged.values():
        row["source_files"] = sorted(set(row["source_files"]))
        row["source_locations"] = sorted(
            row["source_locations"],
            key=lambda item: (item["source_file"], item["line"]),
        )
    return [merged[surface] for surface in sorted(merged)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = merged_executable_korean_literal_inventory(args.source)
    report = {
        "sources": [str(path) for path in args.source],
        "unique_executable_korean_surfaces": len(rows),
        "known_provenance_surfaces": sum(
            row["provenance_article_idx"] != UNKNOWN_PROVENANCE for row in rows
        ),
        "unknown_provenance_surfaces": sum(
            row["provenance_article_idx"] == UNKNOWN_PROVENANCE for row in rows
        ),
        "dev6_specific_surfaces": sum(row["dev6_specific"] for row in rows),
        "disable_flag": DISABLE_DEV6_ENV,
        "rules": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "rules"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


