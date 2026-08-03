"""Build an uncontaminated article-level source-scope judgment sheet."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from ..source_scope_classifier import (
    KOSIS_ORG_ALIASES,
    load_kosis_org_catalog,
)
from .article_claim_pipeline import build_span_candidates


CONTAMINATED_ARTICLE_IDS = frozenset(
    {"380", "1203", "2568", "2615", "2677", "2703"}
)
CLEAN_RESERVED_ARTICLE_IDS = frozenset({"1736", "439"})
JUDGMENT_COLUMNS = (
    "article_idx",
    "기사제목",
    "작성일",
    "current_gold_source_scope",
    "kosis_org_surface_hits",
    "org_attribution_snippets",
    "org_role_summary",
    "value_candidate_count",
    "density_bin",
    "judged_source_scope",
    "judge_note",
)
FORBIDDEN_JUDGMENT_COLUMNS = frozenset({
    "prediction",
    "model_prediction",
    "block_reason",
    "blocking_code",
    "pass",
    "pass_status",
})
ATTRIBUTION_RE = re.compile(
    r"(?:에\s*따르면|가\s*발표한|이\s*발표한|자료|조사|집계|통계|분석)"
)
# ``미 노동부`` is the US Department of Labor, but the alias table maps the bare
# surface ``노동부`` onto 고용노동부.  A foreign marker directly before the
# surface means the sentence is not about the Korean organisation.
FOREIGN_MARKER_RE = re.compile(
    r"(?:미국|美|일본|日|중국|中|영국|英|독일|獨|프랑스|佛|EU|유럽연합|"
    r"러시아|대만|호주|캐나다|인도네시아|인도|베트남|필리핀|태국|싱가포르|"
    r"말레이시아|브라질|멕시코|사우디|아랍에미리트|UAE|튀르키예|터키|"
    r"우크라이나|이스라엘|스페인|이탈리아|네덜란드|스위스|스웨덴|폴란드|"
    r"OECD|IMF|WTO|미)\s*$"
)
FOREIGN_LOOKBEHIND = 8
# A Korean organisation name embedded in a longer word is not a mention of it:
# ``신한은행`` contains ``한은`` but is a different institution.  Korean spacing
# is irregular, so the preceding character is the reliable signal.
HANGUL_RE = re.compile(r"[가-힣]")
# A statistics source publishes figures; an actor merely appears in the news.
# Only the former makes an article a KOSIS stratum candidate.
STATISTICS_SOURCE_RE = re.compile(
    r"(?:에\s*따르면|가\s*발표|이\s*발표|가\s*집계|이\s*집계|"
    r"조사\s*결과|통계|동향|자료를?\s*보면|공표|추계|지수)"
)
ACTOR_RE = re.compile(
    r"(?:세무조사|조사에\s*나선|단속|적발|제재|승인|인가|허가|심의|의결|"
    r"지원|지급|착수|점검|회의|간담회|브리핑|수사|고발|처분|시행|추진)"
)


def _normalize_article_idx(value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def density_bin(count: int) -> str:
    if count < 10:
        return "LOW"
    if count <= 25:
        return "MID"
    return "HIGH"


def _sentences(text: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
        if sentence.strip()
    ]


def _org_surface_index(catalog: dict[str, str]) -> list[tuple[str, str]]:
    pairs: dict[str, str] = {
        name: name for name in catalog.values() if str(name).strip()
    }
    for alias, canonical in KOSIS_ORG_ALIASES.items():
        pairs[str(alias)] = str(canonical)
    return sorted(pairs.items(), key=lambda item: (-len(item[0]), item[0]))


def _is_foreign_mention(text: str, start: int) -> bool:
    return bool(FOREIGN_MARKER_RE.search(text[max(0, start - FOREIGN_LOOKBEHIND):start]))


def _is_embedded_mention(text: str, start: int) -> bool:
    """Whether the surface sits inside a longer Korean word."""
    return start > 0 and bool(HANGUL_RE.fullmatch(text[start - 1]))


def _domestic_occurrences(text: str, surface: str) -> int:
    """Count mentions of ``surface`` that really name the Korean body."""
    domestic = 0
    cursor = text.find(surface)
    while cursor >= 0:
        if not _is_foreign_mention(text, cursor) and not _is_embedded_mention(
            text, cursor
        ):
            domestic += 1
        cursor = text.find(surface, cursor + 1)
    return domestic


def _surface_hits(
    text: str,
    surface_index: list[tuple[str, str]],
) -> list[dict[str, str]]:
    hits = []
    for surface, canonical in surface_index:
        if not surface or surface not in text:
            continue
        if not _domestic_occurrences(text, surface):
            # Every mention was a foreign organisation of the same name.
            continue
        hits.append({"surface": surface, "canonical_org": canonical})
    return hits


def classify_org_role(sentence: str) -> str:
    """Return whether the organisation publishes figures or merely acts."""
    is_source = bool(STATISTICS_SOURCE_RE.search(sentence))
    is_actor = bool(ACTOR_RE.search(sentence))
    if is_source and not is_actor:
        return "통계출처"
    if is_actor and not is_source:
        return "행위주체"
    if is_source and is_actor:
        return "혼재"
    return "불명"


def _attribution_snippets(
    text: str,
    hits: list[dict[str, str]],
    *,
    limit: int = 3,
) -> list[str]:
    surfaces = [row["surface"] for row in hits]
    with_org = [
        sentence for sentence in _sentences(text)
        if any(surface in sentence for surface in surfaces)
    ]
    preferred = [
        sentence for sentence in with_org if ATTRIBUTION_RE.search(sentence)
    ]
    ordered = [*preferred, *with_org]
    result = []
    for sentence in ordered:
        if sentence not in result:
            result.append(sentence)
        if len(result) == limit:
            break
    return result


def org_role_summary(
    text: str,
    hits: list[dict[str, str]],
) -> dict[str, Any]:
    """Summarise how the article uses each detected organisation.

    The reviewer's real question is whether the figures come from a statistic
    the organisation publishes, not whether the name appears at all.  Counting
    source-shaped versus actor-shaped mentions surfaces that distinction
    without deciding it.
    """
    surfaces = [row["surface"] for row in hits]
    roles: Counter[str] = Counter()
    for sentence in _sentences(text):
        if not any(surface in sentence for surface in surfaces):
            continue
        roles[classify_org_role(sentence)] += 1
    return {
        "통계출처": roles["통계출처"],
        "행위주체": roles["행위주체"],
        "혼재": roles["혼재"],
        "불명": roles["불명"],
        "source_shaped": roles["통계출처"] > 0,
    }


def _stable_order(rows: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: hashlib.sha256(
            f"{seed}:{row['article_idx']}".encode("utf-8")
        ).hexdigest(),
    )


def _sample_targets(
    counts: Counter[str],
    sample_size: int,
    min_high: int,
) -> dict[str, int]:
    available_high = counts["HIGH"]
    if available_high < min_high:
        raise ValueError(
            f"HIGH candidates {available_high} < required {min_high}"
        )
    proportional_high = round(sample_size * available_high / sum(counts.values()))
    high = min(available_high, max(min_high, proportional_high))
    remaining = sample_size - high
    low_mid_total = counts["LOW"] + counts["MID"]
    if low_mid_total < remaining:
        extra_high = remaining - low_mid_total
        high += extra_high
        remaining -= extra_high
    if low_mid_total:
        low = min(
            counts["LOW"],
            round(remaining * counts["LOW"] / low_mid_total),
        )
    else:
        low = 0
    mid = remaining - low
    if mid > counts["MID"]:
        low += mid - counts["MID"]
        mid = counts["MID"]
    if low > counts["LOW"]:
        mid += low - counts["LOW"]
        low = counts["LOW"]
    targets = {"LOW": low, "MID": mid, "HIGH": high}
    if sum(targets.values()) != sample_size:
        raise ValueError(f"unable to allocate sample targets: {targets}")
    return targets


def build_stratum_candidates(
    evaluation_paths: list[Path],
    news_path: Path,
    org_catalog_path: Path,
    *,
    sample_size: int = 45,
    min_high: int = 12,
    seed: str = "r17-stratum-20260730",
    require_source_shaped: bool = False,
    exclude_article_ids: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    evaluation_frames = [
        pd.read_csv(
            path,
            encoding="utf-8-sig",
            dtype=str,
            keep_default_na=False,
        )
        for path in evaluation_paths
    ]
    evaluation = pd.concat(evaluation_frames, ignore_index=True)
    evaluation["article_idx"] = evaluation["article_idx"].map(
        _normalize_article_idx
    )
    unknown = evaluation[
        evaluation["gold_source_scope"].eq("불명")
    ].copy()
    scope_by_article = {
        article_idx: "|".join(sorted(set(group["gold_source_scope"])))
        for article_idx, group in evaluation.groupby("article_idx")
    }
    unknown_ids = list(dict.fromkeys(unknown["article_idx"].tolist()))

    news = pd.read_csv(
        news_path,
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False,
    )
    catalog = load_kosis_org_catalog(org_catalog_path)
    surface_index = _org_surface_index(catalog)

    records = []
    for article_idx in unknown_ids:
        if not article_idx.isdigit() or int(article_idx) >= len(news):
            raise ValueError(f"article_idx out of range: {article_idx}")
        article = news.iloc[int(article_idx)]
        body = str(article.get("본문_정제") or "").strip()
        if not body:
            body = str(article.get("기사 본문 전체") or "").strip()
        hits = _surface_hits(body, surface_index)
        value_count = sum(
            candidate.get("kind") == "value_unit"
            for candidate in build_span_candidates(body)
        )
        records.append({
            "article_idx": article_idx,
            "기사제목": article.get("기사제목"),
            "작성일": article.get("작성일"),
            "current_gold_source_scope": scope_by_article.get(
                article_idx, "불명"
            ),
            "kosis_org_surface_hits": hits,
            "org_attribution_snippets": _attribution_snippets(body, hits),
            "org_role_summary": org_role_summary(body, hits),
            "value_candidate_count": value_count,
            "density_bin": density_bin(value_count),
            "judged_source_scope": "",
            "judge_note": "",
            "article_sha256": hashlib.sha256(
                body.encode("utf-8")
            ).hexdigest(),
            "article_text": body,
        })

    eligible = [
        row for row in records if row["kosis_org_surface_hits"]
    ]
    excluded_no_hit = [
        row for row in records if not row["kosis_org_surface_hits"]
    ]
    candidate_pool = [
        row for row in eligible
        if row["article_idx"] not in CONTAMINATED_ARTICLE_IDS
    ]
    if exclude_article_ids:
        candidate_pool = [
            row for row in candidate_pool
            if row["article_idx"] not in exclude_article_ids
        ]
    if require_source_shaped:
        # An article only belongs in the KOSIS stratum when a figure is
        # attributed to a statistic the organisation publishes.  Naming the
        # organisation as an actor is not evidence of that.
        candidate_pool = [
            row for row in candidate_pool
            if (row.get("org_role_summary") or {}).get("source_shaped")
        ]

    def special_record(
        article_idx: str,
        *,
        judged_scope: str,
        note: str,
    ) -> dict[str, Any]:
        article = news.iloc[int(article_idx)]
        body = str(article.get("본문_정제") or "").strip()
        if not body:
            body = str(article.get("기사 본문 전체") or "").strip()
        hits = _surface_hits(body, surface_index)
        value_count = sum(
            candidate.get("kind") == "value_unit"
            for candidate in build_span_candidates(body)
        )
        return {
            "article_idx": article_idx,
            "기사제목": article.get("기사제목"),
            "작성일": article.get("작성일"),
            "current_gold_source_scope": scope_by_article.get(
                article_idx, "KOSIS등재"
            ),
            "kosis_org_surface_hits": hits,
            "org_attribution_snippets": _attribution_snippets(body, hits),
            "org_role_summary": org_role_summary(body, hits),
            "value_candidate_count": value_count,
            "density_bin": density_bin(value_count),
            "judged_source_scope": judged_scope,
            "judge_note": note,
            "article_sha256": hashlib.sha256(
                body.encode("utf-8")
            ).hexdigest(),
            "article_text": body,
        }

    contaminated = [
        special_record(
            article_idx,
            judged_scope="",
            note="기존 개발 오염 기사; 판정 대상 제외",
        )
        for article_idx in sorted(CONTAMINATED_ARTICLE_IDS)
    ]
    clean_reserved = [
        special_record(
            article_idx,
            judged_scope="KOSIS등재",
            note="clean reserved; 판정 대상 제외",
        )
        for article_idx in sorted(CLEAN_RESERVED_ARTICLE_IDS)
    ]

    population_counts = Counter(
        row["density_bin"] for row in candidate_pool
    )
    targets = _sample_targets(population_counts, sample_size, min_high)
    selected = []
    for bin_name in ("LOW", "MID", "HIGH"):
        bin_rows = [
            row for row in candidate_pool
            if row["density_bin"] == bin_name
        ]
        selected.extend(
            _stable_order(bin_rows, seed)[:targets[bin_name]]
        )
    selected = _stable_order(selected, f"{seed}:final")
    public_selected = [
        {column: row[column] for column in JUDGMENT_COLUMNS}
        for row in selected
    ]
    if len(public_selected) != sample_size:
        raise ValueError(
            f"expected {sample_size} judgment rows, got {len(public_selected)}"
        )
    forbidden = FORBIDDEN_JUDGMENT_COLUMNS & set(public_selected[0])
    if forbidden:
        raise ValueError(f"forbidden judgment columns: {sorted(forbidden)}")
    return {
        "selected": public_selected,
        "selected_internal": selected,
        "eligible": eligible,
        "excluded_no_hit": excluded_no_hit,
        "special": {
            "contaminated": contaminated,
            "clean_reserved": clean_reserved,
        },
        "manifest": {
            "contract_version": "l_stratum_judgment_v1",
            "evaluation_rows": len(evaluation),
            "unknown_rows": len(unknown),
            "unknown_unique_articles": len(records),
            "eligible_with_kosis_org_hit": len(eligible),
            "excluded_without_kosis_org_hit": len(excluded_no_hit),
            "contaminated_excluded": len(contaminated),
            "clean_reserved_excluded": len(clean_reserved),
            "candidate_pool": len(candidate_pool),
            "sample_size": len(public_selected),
            "population_density_counts": dict(population_counts),
            "sample_density_targets": targets,
            "sample_density_counts": dict(Counter(
                row["density_bin"] for row in public_selected
            )),
            "sample_seed": seed,
            "judgment_columns": list(JUDGMENT_COLUMNS),
            "forbidden_columns_absent": True,
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    for column in (
        "kosis_org_surface_hits",
        "org_attribution_snippets",
        "org_role_summary",
    ):
        if column in frame:
            frame[column] = frame[column].map(
                lambda value: json.dumps(value, ensure_ascii=False)
            )
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evaluation",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--news", type=Path, required=True)
    parser.add_argument("--org-catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=45)
    parser.add_argument("--min-high", type=int, default=12)
    parser.add_argument("--require-source-shaped", action="store_true")
    parser.add_argument(
        "--exclude-article-ids",
        type=str,
        default="",
        help="comma-separated article_idx values to keep out of the sample",
    )
    args = parser.parse_args()
    excluded = frozenset(
        value.strip()
        for value in args.exclude_article_ids.split(",")
        if value.strip()
    )
    result = build_stratum_candidates(
        args.evaluation,
        args.news,
        args.org_catalog,
        sample_size=args.sample_size,
        min_high=args.min_high,
        require_source_shaped=args.require_source_shaped,
        exclude_article_ids=excluded,
    )
    output_dir = args.output_dir
    _write_csv(output_dir / "stratum_judgment_45.csv", result["selected"])
    _write_jsonl(
        output_dir / "stratum_judgment_45.jsonl",
        result["selected"],
    )
    _write_jsonl(
        output_dir / "stratum_judgment_45_internal.jsonl",
        result["selected_internal"],
    )
    _write_jsonl(
        output_dir / "stratum_eligible_with_org_hit.jsonl",
        result["eligible"],
    )
    _write_jsonl(
        output_dir / "stratum_excluded_no_org_hit.jsonl",
        result["excluded_no_hit"],
    )
    _write_jsonl(
        output_dir / "stratum_contaminated_excluded.jsonl",
        result["special"]["contaminated"],
    )
    _write_jsonl(
        output_dir / "stratum_clean_reserved.jsonl",
        result["special"]["clean_reserved"],
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(result["manifest"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["manifest"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
