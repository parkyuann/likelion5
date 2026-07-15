from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from IPython.display import display


TOP_K_KOSIS = 5
GOLD_FILENAME = "candidate_labeling_pilot_relaxed_team2_codex_prelabel.csv"
KOSIS_SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"


@dataclass(frozen=True)
class MatcherConfig:
    """KOSIS 후보 검색과 평가에 필요한 경로 및 API 설정을 보관합니다."""

    data_dir: Path
    cache_path: Path
    recommendation_path: Path
    kosis_evaluation_detail_path: Path
    kosis_api_key: str
    kosis_search_url: str
    top_k_kosis: int = 5


def find_project_root() -> Path:
    """현재 파일 위치를 기준으로 data 폴더가 있는 프로젝트 루트를 찾습니다."""
    root = Path(__file__).resolve().parents[1]
    if not (root / "data").is_dir():
        raise FileNotFoundError(f"데이터 폴더를 찾을 수 없습니다: {root / 'data'}")
    return root


def build_config(root: Path) -> MatcherConfig:
    """프로젝트 경로와 환경변수로 KOSIS 매칭 설정을 생성합니다."""
    data_dir = root / "data"
    output_dir = root / "output" / "fewshot"
    output_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(root / ".env")
    return MatcherConfig(
        data_dir=data_dir,
        cache_path=output_dir / "hcx_claim_cache.jsonl",
        recommendation_path=output_dir / "fewshot_kosis_recommendations.csv",
        kosis_evaluation_detail_path=(
            output_dir / "fewshot_kosis_top_k_evaluation_detail.csv"
        ),
        kosis_api_key=os.getenv("KOSIS_API_KEY", "").strip(),
        kosis_search_url=KOSIS_SEARCH_URL,
        top_k_kosis=TOP_K_KOSIS,
    )


def load_gold(config: MatcherConfig) -> pd.DataFrame:
    """KOSIS 추천 평가에 사용할 gold CSV를 읽고 기사 번호를 정수로 변환합니다."""
    gold = pd.read_csv(config.data_dir / GOLD_FILENAME, encoding="utf-8-sig")
    gold["article_idx"] = pd.to_numeric(
        gold["article_idx"], errors="raise"
    ).astype(int)
    return gold


def load_claim_cache(path: Path) -> dict[int, dict]:
    """LLM 추출 JSONL 캐시를 검증해 article_idx 기준 딕셔너리로 읽습니다."""
    if not path.is_file():
        raise FileNotFoundError(
            f"주장 캐시를 찾을 수 없습니다: {path}\n"
            "먼저 fewshot_claim_extractor.py를 실행하세요."
        )

    cache = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        item = json.loads(line)
        result = item.get("result") if isinstance(item, dict) else None
        claims = result.get("claims") if isinstance(result, dict) else None
        if "article_idx" not in item or not isinstance(claims, list):
            raise ValueError(f"캐시 {line_number}행의 article_idx/result.claims 구조가 잘못됐습니다.")
        for claim in claims:
            if not isinstance(claim, dict) or not claim.get("claim_text"):
                raise ValueError(f"캐시 {line_number}행에 claim_text가 없는 주장이 있습니다.")
            claim.setdefault("indicator", "")
            claim.setdefault("population", "")
            claim.setdefault("kosis_search_keywords", [])
        cache[int(item["article_idx"])] = item

    if not cache:
        raise ValueError(f"주장 캐시가 비어 있습니다: {path}")
    return cache


def locate_kosis_metadata_file(filename: str, config: MatcherConfig) -> Path:
    """KOSIS 메타데이터 파일을 data 또는 data/통계표에서 찾습니다."""
    candidates = [
        config.data_dir / filename,
        config.data_dir / "통계표" / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{filename}을 찾지 못했습니다.\n" + "\n".join(map(str, candidates)))


def kosis_search(
    keyword: str,
    config: MatcherConfig,
    session: requests.Session,
    result_count=10,
) -> list[dict]:
    """KOSIS 검색 API에서 키워드와 관련된 통계표 후보를 조회합니다."""
    keyword = str(keyword).strip()
    if not keyword:
        return []
    if not config.kosis_api_key:
        raise RuntimeError(".env에 KOSIS_API_KEY가 필요합니다.")
    params = {
        "method": "getList", "apiKey": config.kosis_api_key, "searchNm": keyword,
        "sort": "RANK", "startCount": 1, "resultCount": result_count,
        "format": "json", "jsonVD": "Y",
    }
    response = session.get(config.kosis_search_url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        error_message = str(data.get("errMsg") or "").strip()
        if "데이터가 존재하지 않습니다" in error_message:
            return []
        raise RuntimeError(error_message or str(data))
    return data


def get_field(row, *names):
    """서로 다른 응답 필드명 중 처음 발견한 값을 문자열로 반환합니다."""
    for name in names:
        value = row.get(name) if isinstance(row, dict) else None
        if value not in (None, ""):
            return str(value)
    return ""


def token_set(text):
    """검색과 유사도 계산에 사용할 두 글자 이상의 토큰 집합을 만듭니다."""
    return set(re.findall(r"[가-힣A-Za-z0-9]{2,}", str(text).lower()))


def load_local_kosis_catalog(tree_path: Path, org_path: Path):
    """KOSIS 트리와 기관명 JSON을 검색 가능한 카탈로그로 변환합니다."""
    tree = json.loads(Path(tree_path).read_text(encoding="utf-8"))
    org_names = json.loads(Path(org_path).read_text(encoding="utf-8"))
    rows = []
    roots = tree.values() if isinstance(tree, dict) else tree
    for root in roots:
        if not isinstance(root, dict):
            continue
        for leaf in root.get("leaves", []):
            org_id = str(leaf.get("org_id", leaf.get("ORG_ID", "")))
            org_name = str(
                org_names.get(org_id, "") if isinstance(org_names, dict) else ""
            )
            rows.append({
                "ORG_ID": org_id,
                "ORG_NM": org_name,
                "TBL_ID": str(leaf.get("tbl_id", leaf.get("TBL_ID", ""))),
                "TBL_NM": str(leaf.get("tbl_nm", leaf.get("TBL_NM", ""))),
                "STAT_ID": str(leaf.get("stat_id", leaf.get("STAT_ID", ""))),
                "CATEGORY_PATH": " > ".join(map(str, leaf.get("path", []))),
                "candidate_source": "local_json",
            })
    catalog = pd.DataFrame(rows).drop_duplicates(["ORG_ID", "TBL_ID"]).reset_index(drop=True)
    catalog["SEARCH_TEXT"] = (
        catalog["ORG_NM"] + " " + catalog["TBL_NM"] + " " + catalog["CATEGORY_PATH"]
    ).str.lower()
    print(f"로컬 KOSIS 후보 {len(catalog):,}개 로드")
    return catalog


def search_local_kosis(query: str, catalog: pd.DataFrame, result_count=30) -> list[dict]:
    """로컬 KOSIS 카탈로그에서 토큰 일치 점수가 높은 후보를 찾습니다."""
    tokens = token_set(query) - {
        "통계", "자료", "관련", "기준", "전국", "대한", "따르면", "지난해", "올해"
    }
    if not tokens or catalog.empty:
        return []
    scores = pd.Series(0.0, index=catalog.index)
    for token in tokens:
        scores += catalog["SEARCH_TEXT"].str.contains(
            re.escape(token), regex=True, na=False
        ).astype(float) * (1.0 + min(len(token), 8) / 8)
    selected = scores[scores > 0].nlargest(result_count)
    result = catalog.loc[selected.index].drop(columns=["SEARCH_TEXT"]).copy()
    result["local_retrieval_score"] = selected.to_numpy()
    return result.to_dict("records")


def normalize_api_candidate(row: dict) -> dict:
    """KOSIS API 후보의 필드명을 로컬 카탈로그 형식으로 통일합니다."""
    return {
        "ORG_ID": get_field(row, "ORG_ID", "orgId", "org_id"),
        "ORG_NM": get_field(row, "ORG_NM", "orgNm", "org_name"),
        "TBL_ID": get_field(row, "TBL_ID", "tblId", "tbl_id"),
        "TBL_NM": get_field(row, "TBL_NM", "tblNm", "tbl_name"),
        "STAT_ID": get_field(row, "STAT_ID", "statId", "stat_id"),
        "CATEGORY_PATH": get_field(row, "CATEGORY_PATH", "category_path"),
        "candidate_source": "kosis_api",
        "local_retrieval_score": 0.0,
    }


def merge_kosis_candidates(local_rows, api_rows, catalog_by_table):
    """로컬 후보와 API 후보를 기관·통계표 기준으로 병합합니다."""
    merged = {}
    for raw in [*local_rows, *[normalize_api_candidate(row) for row in api_rows]]:
        table_id = get_field(raw, "TBL_ID")
        org_id = get_field(raw, "ORG_ID")
        key = (org_id, table_id) if org_id or table_id else None
        if key is None:
            continue
        local_match = catalog_by_table.get(table_id, {})
        candidate = {
            "ORG_ID": get_field(raw, "ORG_ID") or get_field(local_match, "ORG_ID"),
            "ORG_NM": get_field(raw, "ORG_NM") or get_field(local_match, "ORG_NM"),
            "TBL_ID": table_id,
            "TBL_NM": get_field(raw, "TBL_NM") or get_field(local_match, "TBL_NM"),
            "STAT_ID": get_field(raw, "STAT_ID") or get_field(local_match, "STAT_ID"),
            "CATEGORY_PATH": get_field(raw, "CATEGORY_PATH") or get_field(local_match, "CATEGORY_PATH"),
            "local_retrieval_score": float(raw.get("local_retrieval_score", 0) or 0),
            "from_local_json": raw.get("candidate_source") == "local_json",
            "from_kosis_api": raw.get("candidate_source") == "kosis_api",
        }
        if key in merged:
            previous = merged[key]
            for field in ["ORG_NM", "TBL_NM", "STAT_ID", "CATEGORY_PATH"]:
                previous[field] = previous[field] or candidate[field]
            previous["local_retrieval_score"] = max(
                previous["local_retrieval_score"], candidate["local_retrieval_score"]
            )
            previous["from_local_json"] |= candidate["from_local_json"]
            previous["from_kosis_api"] |= candidate["from_kosis_api"]
        else:
            merged[key] = candidate
    return list(merged.values())


def rank_kosis_candidates(claim, candidates):
    """지표·기관·경로 유사도를 결합해 KOSIS 후보의 순위를 계산합니다."""
    indicator = str(claim.get("indicator", ""))
    population = str(claim.get("population", ""))
    keywords = claim.get("kosis_search_keywords", []) or []
    expected_org = str(claim.get("expected_kosis_org", "")).strip()
    mentioned_org = str(claim.get("source_org_mentioned", "")).strip()
    query = " ".join([indicator, population, *map(str, keywords)]).strip()
    query_tokens = token_set(query)
    indicator_tokens = token_set(indicator)
    ranked = []
    for row in candidates:
        table_name = get_field(row, "TBL_NM")
        org_name = get_field(row, "ORG_NM")
        category_path = get_field(row, "CATEGORY_PATH")
        table_tokens = token_set(table_name)
        path_tokens = token_set(category_path)
        indicator_score = len(query_tokens & table_tokens) / max(1, len(query_tokens))
        if indicator and table_name:
            indicator_score = max(
                indicator_score,
                SequenceMatcher(None, indicator.lower(), table_name.lower()).ratio(),
            )
        org_targets = [value for value in [expected_org, mentioned_org] if value]
        org_score = max([
            1.0 if target in org_name or org_name in target else
            SequenceMatcher(None, target, org_name).ratio()
            for target in org_targets
        ], default=0.0)
        path_score = len((indicator_tokens or query_tokens) & path_tokens) / max(
            1, len(indicator_tokens or query_tokens)
        )
        api_score = 1.0 if row.get("from_kosis_api") else 0.0
        local_score = min(float(row.get("local_retrieval_score", 0)) / 10, 1.0)
        final_score = (
            0.50 * indicator_score + 0.25 * org_score
            + 0.15 * path_score + 0.05 * api_score + 0.05 * local_score
        )
        ranked.append({
            **row,
            "indicator_score": round(indicator_score, 4),
            "org_score": round(org_score, 4),
            "path_score": round(path_score, 4),
            "_score": round(final_score, 4),
        })
    return sorted(ranked, key=lambda row: row["_score"], reverse=True)


def recommend_kosis(cache, config: MatcherConfig):
    """추출 주장별로 로컬 검색과 KOSIS API를 결합한 Top-k 후보를 만듭니다."""
    table_tree_path = locate_kosis_metadata_file("kosis_table_tree.json", config)
    org_names_path = locate_kosis_metadata_file("kosis_org_names.json", config)
    print("KOSIS table tree:", table_tree_path)
    print("KOSIS org names:", org_names_path)
    local_catalog = load_local_kosis_catalog(table_tree_path, org_names_path)
    catalog_by_table = {
        str(row.TBL_ID): row._asdict()
        for row in local_catalog.drop(columns=["SEARCH_TEXT"]).itertuples(index=False)
    }
    api_cache = {}
    output_rows = []
    with requests.Session() as session:
        for article_idx, item in cache.items():
            for claim_no, claim in enumerate(item["result"].get("claims", []), 1):
                keywords = [
                    str(keyword).strip()
                    for keyword in claim.get("kosis_search_keywords", [])[:3]
                    if str(keyword).strip()
                ]
                combined_query = " ".join([
                    str(claim.get("indicator", "")),
                    str(claim.get("population", "")),
                    *keywords,
                ]).strip()
                local_rows = search_local_kosis(
                    combined_query, local_catalog, result_count=40
                )
                api_rows = []
                for keyword in keywords:
                    if keyword not in api_cache:
                        api_cache[keyword] = kosis_search(
                            keyword, config, session, result_count=10
                        )
                        time.sleep(0.15)
                    api_rows.extend(api_cache[keyword])
                merged = merge_kosis_candidates(
                    local_rows, api_rows, catalog_by_table
                )
                ranked = rank_kosis_candidates(claim, merged)[:config.top_k_kosis]
                if not ranked:
                    output_rows.append({
                        "article_idx": article_idx, "claim_no": claim_no,
                        "claim_text": claim.get("claim_text", ""),
                        "indicator": claim.get("indicator", ""),
                        "rank": "", "score": "", "org_id": "", "org_name": "",
                        "tbl_id": "", "tbl_name": "", "stat_id": "",
                        "category_path": "", "indicator_score": "",
                        "org_score": "", "path_score": "",
                        "from_local_json": False, "from_kosis_api": False,
                    })
                    continue
                for rank, row in enumerate(ranked, 1):
                    output_rows.append({
                        "article_idx": article_idx, "claim_no": claim_no,
                        "claim_text": claim.get("claim_text", ""),
                        "indicator": claim.get("indicator", ""),
                        "rank": rank, "score": row["_score"],
                        "org_id": get_field(row, "ORG_ID"),
                        "org_name": get_field(row, "ORG_NM"),
                        "tbl_id": get_field(row, "TBL_ID"),
                        "tbl_name": get_field(row, "TBL_NM"),
                        "stat_id": get_field(row, "STAT_ID"),
                        "category_path": get_field(row, "CATEGORY_PATH"),
                        "indicator_score": row["indicator_score"],
                        "org_score": row["org_score"],
                        "path_score": row["path_score"],
                        "from_local_json": row.get("from_local_json", False),
                        "from_kosis_api": row.get("from_kosis_api", False),
                    })
    return pd.DataFrame(output_rows)

def similarity(a, b):
    """두 텍스트를 정규화한 뒤 SequenceMatcher 유사도를 계산합니다."""
    a = re.sub(r"[^0-9a-z가-힣]", "", str(a).lower())
    b = re.sub(r"[^0-9a-z가-힣]", "", str(b).lower())
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0

def clean_text(value):
    """결측값을 빈 문자열로 바꾸고 나머지 텍스트의 공백을 제거합니다."""
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def compact_unique(values):
    """값의 입력 순서를 유지하면서 빈 값과 중복을 제거합니다."""
    result = []
    for value in values:
        text = str(value).strip()
        if text and text.lower() != "nan" and text not in result:
            result.append(text)
    return result


def build_kosis_evaluation_detail(scorable_rows, recommendation_rows):
    """기관명·지표명 gold와 KOSIS Top-k 후보의 적중 상세를 생성합니다."""
    detail_rows = []
    recommendations_by_article = (
        {
            str(article_idx): group
            for article_idx, group in recommendation_rows.groupby("article_idx")
        }
        if not recommendation_rows.empty
        else {}
    )
    for _, gold_row in scorable_rows.iterrows():
        article_key = str(gold_row.get("article_idx", ""))
        article_recommendations = recommendations_by_article.get(
            article_key, pd.DataFrame()
        )

        gold_org = clean_text(gold_row.get("gold_source_org_raw", ""))
        gold_indicator = clean_text(gold_row.get("gold_indicator_raw", ""))
        candidate_details = []
        org_hit = False
        indicator_hit = False
        maximum_similarity = 0.0

        for recommendation in article_recommendations.itertuples(index=False):
            predicted_org = clean_text(getattr(recommendation, "org_name", ""))
            predicted_table = clean_text(getattr(recommendation, "tbl_name", ""))
            predicted_table_id = clean_text(getattr(recommendation, "tbl_id", ""))
            current_org_hit = bool(gold_org and predicted_org and gold_org in predicted_org)
            current_similarity = similarity(gold_indicator, predicted_table) if gold_indicator and predicted_table else 0.0
            current_indicator_hit = bool(gold_indicator and current_similarity >= 0.45)
            org_hit = org_hit or current_org_hit
            indicator_hit = indicator_hit or current_indicator_hit
            maximum_similarity = max(maximum_similarity, current_similarity)
            candidate_details.append({
                "rank": getattr(recommendation, "rank", ""),
                "org_name": predicted_org,
                "tbl_id": predicted_table_id,
                "tbl_name": predicted_table,
                "org_hit": current_org_hit,
                "indicator_similarity": round(current_similarity, 4),
                "indicator_hit": current_indicator_hit,
            })

        valid_candidates = [
            candidate for candidate in candidate_details
            if candidate["org_name"] or candidate["tbl_id"] or candidate["tbl_name"]
        ]
        detail_rows.append({
            "sample_id": gold_row.get("sample_id", ""),
            "article_idx": gold_row.get("article_idx", ""),
            "기사제목": gold_row.get("기사제목", ""),
            "claim_text": gold_row.get("claim_text", ""),
            "gold_source_org_raw": gold_org,
            "gold_indicator_raw": gold_indicator,
            "evaluation_basis": "+".join([
                label for condition, label in [
                    (bool(gold_org), "기관명"), (bool(gold_indicator), "지표명")
                ] if condition
            ]),
            "recommendation_count": len(valid_candidates),
            "recommended_org_names": " | ".join(compact_unique(
                candidate["org_name"] for candidate in valid_candidates
            )),
            "recommended_table_ids": " | ".join(compact_unique(
                candidate["tbl_id"] for candidate in valid_candidates
            )),
            "recommended_table_names": " | ".join(compact_unique(
                candidate["tbl_name"] for candidate in valid_candidates
            )),
            "max_indicator_similarity": round(maximum_similarity, 4),
            "hit_by_org": org_hit,
            "hit_by_indicator": indicator_hit,
            "kosis_top_k_hit": org_hit or indicator_hit,
            "top_k_candidates_json": json.dumps(valid_candidates, ensure_ascii=False),
        })
    return pd.DataFrame(detail_rows)


def evaluate_kosis_recommendations(gold, recommendations, config: MatcherConfig):
    """평가 가능한 gold 행에 대해 KOSIS Top-k 적중률을 계산하고 저장합니다."""
    gold_org = gold.get(
        "gold_source_org_raw", pd.Series(index=gold.index, dtype=str)
    )
    gold_indicator = gold.get(
        "gold_indicator_raw", pd.Series(index=gold.index, dtype=str)
    )
    gold_org_available = gold_org.fillna("").astype(str).str.strip().ne("")
    gold_indicator_available = (
        gold_indicator.fillna("").astype(str).str.strip().ne("")
    )
    scorable_mask = gold_org_available | gold_indicator_available
    scorable = gold[scorable_mask].copy()
    not_scorable = gold[~scorable_mask].copy()

    if scorable.empty:
        kosis_top_k_detail = pd.DataFrame()
        print("기관명 또는 지표명 정답이 있는 gold 행이 없어 Top-k 평가를 생략합니다.")
    else:
        kosis_top_k_detail = build_kosis_evaluation_detail(scorable, recommendations)
        hit_rate = (
            float(kosis_top_k_detail["kosis_top_k_hit"].mean())
            if len(kosis_top_k_detail)
            else 0.0
        )
        print({
            "kosis_top_k_evaluable": len(kosis_top_k_detail),
            "kosis_top_k_hit_rate": hit_rate,
        })
        print("아래 행들이 실제 Top-k 평가 분모에 포함됐습니다.")
        display(kosis_top_k_detail[[
            "sample_id", "article_idx", "기사제목", "claim_text",
            "gold_source_org_raw", "gold_indicator_raw", "evaluation_basis",
            "recommendation_count", "recommended_org_names", "recommended_table_ids",
            "recommended_table_names", "max_indicator_similarity",
            "hit_by_org", "hit_by_indicator", "kosis_top_k_hit",
        ]])
        kosis_top_k_detail.to_csv(
            config.kosis_evaluation_detail_path, index=False, encoding="utf-8-sig"
        )
        print("평가 상세 저장:", config.kosis_evaluation_detail_path)

    if not not_scorable.empty:
        print(f"기관명과 지표명 정답이 모두 없어 평가에서 제외된 gold 행: {len(not_scorable)}개")
        excluded_columns = [
            column for column in ["sample_id", "article_idx", "기사제목", "claim_text"]
            if column in not_scorable.columns
        ]
        display(
            not_scorable[excluded_columns].assign(
                exclusion_reason="gold 기관명·지표명 모두 없음"
            )
        )
    return kosis_top_k_detail

def run_matching(gold, claim_cache, config: MatcherConfig):
    """추출된 주장에 KOSIS 후보를 추천하고 Top-k 평가 결과를 저장합니다."""
    recommendations = recommend_kosis(claim_cache, config)
    recommendations.to_csv(
        config.recommendation_path, index=False, encoding="utf-8-sig"
    )
    display(recommendations.head(10))
    kosis_top_k_detail = evaluate_kosis_recommendations(
        gold, recommendations, config
    )
    return {
        "recommendations": recommendations,
        "kosis_top_k_detail": kosis_top_k_detail,
    }


def main():
    """gold와 LLM 캐시를 준비한 뒤 KOSIS 매칭 단계를 실행합니다."""
    root = find_project_root()
    config = build_config(root)
    print({
        "KOSIS key loaded": bool(config.kosis_api_key),
        "top_k": config.top_k_kosis,
    })
    gold = load_gold(config)
    claim_cache = load_claim_cache(config.cache_path)
    return run_matching(gold, claim_cache, config)


if __name__ == "__main__":
    main()
