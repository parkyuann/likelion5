from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from IPython.display import display


DRY_RUN = False
MAX_ARTICLES = None
TOP_K_KOSIS = 5
ARTICLE_FILENAME = "AI_기반_뉴스_사실검증_시스템_프로젝트_데이터_본문전처리.csv"
GOLD_FILENAME = "candidate_labeling_pilot_relaxed_team2_codex_prelabel.csv"
KOSIS_SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"


@dataclass(frozen=True)
class PipelineConfig:
    """실행 중 공통으로 사용하는 경로, API, 처리량 설정을 보관합니다."""

    data_dir: Path
    cache_path: Path
    result_path: Path
    recommendation_path: Path
    kosis_evaluation_detail_path: Path
    hcx_api_key: str
    kosis_api_key: str
    hcx_url: str
    kosis_search_url: str
    dry_run: bool = True
    max_articles: int | None = None
    top_k_kosis: int = 5


def find_project_root() -> Path:
    """현재 파일 위치를 기준으로 data 폴더가 있는 프로젝트 루트를 찾습니다."""
    root = Path(__file__).resolve().parents[1]
    if not (root / "data").is_dir():
        raise FileNotFoundError(f"데이터 폴더를 찾을 수 없습니다: {root / 'data'}")
    return root


def build_config(root: Path) -> PipelineConfig:
    """프로젝트 경로와 환경변수로 파이프라인 설정을 생성합니다."""
    data_dir = root / "data"
    output_dir = root / "output" / "fewshot"
    output_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(root / ".env")
    hcx_model = os.getenv("HCX_MODEL", "HCX-005")
    return PipelineConfig(
        data_dir=data_dir,
        cache_path=output_dir / "hcx_claim_cache.jsonl",
        result_path=output_dir / "fewshot_hcx_predictions.csv",
        recommendation_path=output_dir / "fewshot_kosis_recommendations.csv",
        kosis_evaluation_detail_path=(
            output_dir / "fewshot_kosis_top_k_evaluation_detail.csv"
        ),
        hcx_api_key=os.getenv("NCP_CLOVASTUDIO_API_KEY", "").strip(),
        kosis_api_key=os.getenv("KOSIS_API_KEY", "").strip(),
        hcx_url=(
            "https://clovastudio.stream.ntruss.com/"
            f"v3/chat-completions/{hcx_model}"
        ),
        kosis_search_url=KOSIS_SEARCH_URL,
        dry_run=DRY_RUN,
        max_articles=MAX_ARTICLES,
        top_k_kosis=TOP_K_KOSIS,
    )


def load_evaluation_data(config: PipelineConfig):
    """data 폴더에서 기사와 gold CSV를 읽어 평가 대상 기사만 선택합니다."""
    articles = pd.read_csv(
        config.data_dir / ARTICLE_FILENAME, encoding="utf-8-sig"
    )
    gold = pd.read_csv(config.data_dir / GOLD_FILENAME, encoding="utf-8-sig")
    articles = articles.reset_index().rename(columns={"index": "article_idx"})
    articles["article_idx"] = articles["article_idx"].astype(int)
    gold["article_idx"] = pd.to_numeric(
        gold["article_idx"], errors="raise"
    ).astype(int)

    target_ids = gold["article_idx"].drop_duplicates()
    target_articles = articles[articles["article_idx"].isin(target_ids)].copy()
    target_articles = target_articles.sort_values("article_idx").reset_index(drop=True)
    missing_ids = sorted(set(target_ids) - set(target_articles["article_idx"]))
    if missing_ids:
        raise ValueError(f"본문 파일에서 article_idx를 찾지 못함: {missing_ids}")
    if not target_articles["본문_정제"].notna().all():
        raise ValueError("본문_정제 결측치가 있습니다.")

    print(
        f"후보 행: {len(gold):,} / 고유 평가 기사: {len(target_articles):,} "
        f"/ 전체 기사: {len(articles):,}"
    )
    display(
        target_articles[
            ["article_idx", "기사제목", "작성일", "본문_길이"]
        ].head()
    )
    return target_articles, gold


SYSTEM_PROMPT = r'''당신은 한국 뉴스의 수치 기반 주장을 구조화하는 데이터 분석가다.
기사에서 검증 가능한 집계통계 주장만 추출하고 KOSIS 검색 계획을 만든다.

[집계통계 포함]
- 인구, 고용, 물가, 무역, 복지, 교육 등 집단/기간을 집계한 관측 통계
- 평균, 비율, 증감, 순위, 규모 등 공식 통계표로 확인 가능한 주장

[제외]
- 개별 기업의 매출/투자/생산 계획, 주가/지분 거래
- 사건 1건, 개인 1명, 법령의 기준값, 정책 목표/전망/여론조사
- 행사 안내, 단순 날짜/가격/제품 사양

반드시 JSON 객체 하나만 출력한다. 설명이나 코드펜스를 쓰지 않는다.
스키마:
{"claims":[{
 "claim_text":"기사 원문에서 완결된 문장 그대로",
 "is_aggregate_claim":true,
 "claim_class":"집계통계",
 "indicator":"통계 지표명",
 "population":"대상 집단/지역",
 "value":"수치(복수면 ; 구분)",
 "unit":"단위(복수면 ; 구분)",
 "time_ref":"기준 시점",
 "time_compare":"비교 시점/대상",
 "source_org_mentioned":"기사에 명시된 기관 또는 빈 문자열",
 "expected_kosis_org":"KOSIS에서 예상되는 작성기관 또는 빈 문자열",
 "kosis_search_keywords":["짧고 구체적인 검색어 1","검색어 2"],
 "reason":"포함 근거"
}],"excluded":[{"text":"제외한 수치 문장","class":"개별사례|목표계획|전망예측|법령제도|여론조사|기타","reason":"짧은 이유"}]}

집계통계가 없으면 claims는 반드시 []로 둔다. 기관이나 통계표 ID를 지어내지 않는다.'''

FEW_SHOT = [
    ({"title": "취업자 증가", "text": "통계청에 따르면 지난달 취업자는 2,900만명으로 전년 동월보다 18만명 늘었다."},
     {"claims": [{"claim_text": "통계청에 따르면 지난달 취업자는 2,900만명으로 전년 동월보다 18만명 늘었다.", "is_aggregate_claim": True, "claim_class": "집계통계", "indicator": "취업자 수", "population": "전국 취업자", "value": "2900만;18만", "unit": "명;명", "time_ref": "지난달", "time_compare": "전년 동월", "source_org_mentioned": "통계청", "expected_kosis_org": "통계청", "kosis_search_keywords": ["취업자 수", "고용동향 취업자"], "reason": "전국 고용 집계의 시계열 수치"}], "excluded": []}),
    ({"title": "공장 증설", "text": "A사는 내년까지 4천억원을 투자해 생산량을 30% 늘릴 계획이다."},
     {"claims": [], "excluded": [{"text": "A사는 내년까지 4천억원을 투자해 생산량을 30% 늘릴 계획이다.", "class": "목표계획", "reason": "개별 기업의 미래 투자 계획"}]}),
    ({"title": "소비자물가", "text": "지난해 소비자물가는 1년 전보다 2.3% 올랐고 농축수산물은 5.9% 상승했다."},
     {"claims": [{"claim_text": "지난해 소비자물가는 1년 전보다 2.3% 올랐고 농축수산물은 5.9% 상승했다.", "is_aggregate_claim": True, "claim_class": "집계통계", "indicator": "소비자물가지수 상승률", "population": "전국", "value": "2.3;5.9", "unit": "%;%", "time_ref": "지난해", "time_compare": "1년 전", "source_org_mentioned": "", "expected_kosis_org": "통계청", "kosis_search_keywords": ["소비자물가지수", "농축수산물 소비자물가"], "reason": "공식 물가 집계로 확인 가능한 증감률"}], "excluded": []}),
]

def build_messages(row):
    """기사 한 건과 few-shot 예시를 HCX 대화 메시지로 구성합니다."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for x, y in FEW_SHOT:
        messages += [
            {"role": "user", "content": json.dumps(x, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(y, ensure_ascii=False)},
        ]
    payload = {"article_idx": int(row.article_idx), "title": row.기사제목,
               "date": str(row.작성일), "text": row.본문_정제}
    messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False)})
    return messages

def extract_json_object(text: str) -> dict:
    """모델 응답에서 JSON 객체를 추출하고 흔한 형식 오류를 보정합니다."""
    text = text.strip().lstrip('\ufeff')
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"JSON 객체를 찾지 못함: {text[:200]}")
    candidate = text[start:end + 1]
    # 흔한 생성 오류(후행 쉼표, 제어문자)를 먼저 정리합니다.
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    candidate = ''.join(ch if ord(ch) >= 32 or ch in '\n\r\t' else ' ' for ch in candidate)
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError as error:
        context = candidate[max(0, error.pos-80):error.pos+80]
        raise ValueError(
            f"HCX JSON 문법 오류({error.msg}, 위치 {error.pos}). 주변 내용: {context!r}"
        ) from error

def validate_result(obj: dict) -> dict:
    """추출 결과가 필수 claims 구조와 claim_text를 갖췄는지 확인합니다."""
    if not isinstance(obj, dict) or not isinstance(obj.get("claims"), list):
        raise ValueError("응답에 claims 배열이 없습니다.")
    obj.setdefault("excluded", [])
    for claim in obj["claims"]:
        if not claim.get("claim_text"):
            raise ValueError("claim_text가 비어 있습니다.")
        claim.setdefault("kosis_search_keywords", [])
    return obj

def hcx_chat(messages, config: PipelineConfig, session: requests.Session, retries=4):
    """HCX를 호출하고 필요하면 깨진 JSON을 복구해 구조화 결과를 반환합니다."""
    if not config.hcx_api_key:
        raise RuntimeError(".env에 NCP_CLOVASTUDIO_API_KEY가 필요합니다.")
    headers = {
        "Authorization": f"Bearer {config.hcx_api_key}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"messages": messages, "temperature": 0.0, "topP": 0.8,
            "topK": 0, "maxTokens": 1800, "repetitionPenalty": 1.05, "stop": []}
    for attempt in range(retries):
        try:
            response = session.post(config.hcx_url, headers=headers, json=body, timeout=120)
            response.raise_for_status()
            data = response.json()
            content = (data.get("result", {}).get("message", {}).get("content")
                       or data.get("message", {}).get("content"))
            if isinstance(content, list):
                content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
            if not content:
                raise ValueError(f"HCX 응답 본문 구조 확인 필요: {str(data)[:500]}")
            usage = data.get("result", {}).get("usage", data.get("usage", {}))
            try:
                parsed = extract_json_object(content)
            except ValueError as parse_error:
                # 따옴표 escape 등이 깨졌다면 모델에 JSON 재직렬화를 한 번 요청합니다.
                repair_body = {**body, "messages": [
                    {"role": "system", "content": (
                        "입력 내용을 변경·요약하지 말고 유효한 JSON 객체 하나로만 재직렬화하라. "
                        "문자열 내부의 큰따옴표와 줄바꿈을 반드시 JSON 규칙에 맞게 escape하라. "
                        "코드펜스와 설명은 출력하지 마라."
                    )},
                    {"role": "user", "content": content},
                ], "maxTokens": 2000}
                repair_headers = {**headers, "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4())}
                repaired_response = session.post(
                    config.hcx_url, headers=repair_headers, json=repair_body, timeout=120
                )
                repaired_response.raise_for_status()
                repaired_data = repaired_response.json()
                repaired_content = (
                    repaired_data.get("result", {}).get("message", {}).get("content")
                    or repaired_data.get("message", {}).get("content")
                )
                if isinstance(repaired_content, list):
                    repaired_content = "".join(
                        x.get("text", "") for x in repaired_content if isinstance(x, dict)
                    )
                if not repaired_content:
                    raise parse_error
                parsed = extract_json_object(repaired_content)
            return validate_result(parsed), usage
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)

def load_cache(path: Path):
    """JSONL 캐시를 article_idx 기준 딕셔너리로 읽습니다."""
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                cache[int(item["article_idx"])] = item
    return cache

def append_cache(item, path: Path):
    """기사 한 건의 HCX 결과를 JSONL 캐시에 추가합니다."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

def run_hcx(rows, config: PipelineConfig):
    """캐시에 없는 기사만 HCX로 처리하고 결과를 누적합니다."""
    cache = load_cache(config.cache_path)
    work = rows.head(config.max_articles) if config.max_articles else rows
    with requests.Session() as session:
        for i, row in enumerate(work.itertuples(index=False), 1):
            idx = int(row.article_idx)
            if idx in cache:
                continue
            result, usage = hcx_chat(build_messages(row), config, session)
            item = {"article_idx": idx, "result": result, "usage": usage}
            append_cache(item, config.cache_path)
            cache[idx] = item
            print(f"[{i}/{len(work)}] article_idx={idx}, claims={len(result['claims'])}")
    return cache


def locate_kosis_metadata_file(filename: str, config: PipelineConfig) -> Path:
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
    config: PipelineConfig,
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


def recommend_kosis(cache, config: PipelineConfig):
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


def flatten_predictions(cache):
    """기사별 캐시의 claims를 평가 가능한 행 단위 데이터프레임으로 펼칩니다."""
    rows = []
    for article_idx, item in cache.items():
        for claim_no, claim in enumerate(item["result"]["claims"], 1):
            rows.append({"article_idx": article_idx, "pred_claim_no": claim_no, **claim})
    return pd.DataFrame(rows)

def greedy_match(gold_df, pred_df, threshold=0.55):
    """기사별 텍스트 유사도가 높은 gold와 예측 claim을 일대일 매칭합니다."""
    pairs = []
    for article_idx, gpart in gold_df.groupby("article_idx"):
        ppart = pred_df[pred_df["article_idx"] == article_idx]
        candidates = [(similarity(g.claim_text, p.claim_text), gi, pi)
                      for gi, g in gpart.iterrows() for pi, p in ppart.iterrows()]
        used_g, used_p = set(), set()
        for score, gi, pi in sorted(candidates, reverse=True):
            if score < threshold or gi in used_g or pi in used_p:
                continue
            used_g.add(gi)
            used_p.add(pi)
            pairs.append({"gold_index": gi, "pred_index": pi, "text_similarity": score})
    return pd.DataFrame(pairs)


def evaluate_claims(gold, hcx_cache, recommendations, config: PipelineConfig):
    """추출 claim을 gold와 비교해 분류 지표와 상세 결과를 저장합니다."""
    pred = flatten_predictions(hcx_cache)
    matches = greedy_match(gold, pred) if not pred.empty else pd.DataFrame()
    print(f"예측 claim {len(pred):,}개 / 매칭 {len(matches):,}개")

    gold_eval = gold.copy()
    gold_eval["gold_bool"] = gold_eval["gold_is_aggregate_claim"].map(
        lambda value: str(value).strip().lower() in {"true", "1", "yes", "y"}
    )
    matched_gold = set(matches["gold_index"].tolist()) if not matches.empty else set()
    matched_pred = set(matches["pred_index"].tolist()) if not matches.empty else set()
    positive_gold = set(gold_eval.index[gold_eval["gold_bool"]])

    tp = len(matched_gold & positive_gold)
    fp = len(pred) - len(matched_pred) + len(matched_gold - positive_gold)
    fn = len(positive_gold - matched_gold)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    gold_eval["pred_is_aggregate_claim"] = gold_eval.index.isin(matched_gold)
    accuracy = (gold_eval["gold_bool"] == gold_eval["pred_is_aggregate_claim"]).mean()

    metrics = pd.DataFrame([{
        "gold_positive": len(positive_gold), "predicted_claims": len(pred),
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1,
        "candidate_level_accuracy": accuracy,
    }])
    display(metrics.round(4))

    detail = gold_eval.copy()
    detail["matched"] = detail.index.isin(matched_gold)
    detail["matched_pred_claim"] = ""
    detail["text_similarity"] = 0.0
    if not matches.empty:
        for match in matches.itertuples(index=False):
            detail.loc[match.gold_index, "matched_pred_claim"] = pred.loc[
                match.pred_index, "claim_text"
            ]
            detail.loc[match.gold_index, "text_similarity"] = match.text_similarity
    detail.to_csv(config.result_path, index=False, encoding="utf-8-sig")

    print("상세 비교:", config.result_path)
    if not recommendations.empty:
        print("KOSIS 추천:", config.recommendation_path)
    return gold_eval, metrics, detail


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


def evaluate_kosis_recommendations(gold_eval, recommendations, config: PipelineConfig):
    """평가 가능한 gold 행에 대해 KOSIS Top-k 적중률을 계산하고 저장합니다."""
    gold_org = gold_eval.get(
        "gold_source_org_raw", pd.Series(index=gold_eval.index, dtype=str)
    )
    gold_indicator = gold_eval.get(
        "gold_indicator_raw", pd.Series(index=gold_eval.index, dtype=str)
    )
    gold_org_available = gold_org.fillna("").astype(str).str.strip().ne("")
    gold_indicator_available = (
        gold_indicator.fillna("").astype(str).str.strip().ne("")
    )
    scorable_mask = gold_org_available | gold_indicator_available
    scorable = gold_eval[scorable_mask].copy()
    not_scorable = gold_eval[~scorable_mask].copy()

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


def run_pipeline(target_articles, gold, config: PipelineConfig):
    """HCX 추출부터 KOSIS 추천과 평가까지 전체 실험을 실행합니다."""
    if target_articles.empty:
        raise ValueError("평가할 기사가 없습니다.")

    print(build_messages(target_articles.iloc[0])[-1]["content"][:500])

    if config.dry_run:
        print("DRY_RUN=True: 호출하지 않았습니다. 프롬프트를 확인한 뒤 False로 변경하세요.")
        hcx_cache = load_cache(config.cache_path)
    else:
        hcx_cache = run_hcx(target_articles, config)

    if config.dry_run or not hcx_cache:
        recommendations = pd.DataFrame()
        print("HCX 캐시 결과가 생긴 뒤 KOSIS 추천을 실행합니다.")
    else:
        recommendations = recommend_kosis(hcx_cache, config)
        recommendations.to_csv(
            config.recommendation_path, index=False, encoding="utf-8-sig"
        )
        display(recommendations.head(10))

    gold_eval, metrics, detail = evaluate_claims(
        gold, hcx_cache, recommendations, config
    )
    kosis_top_k_detail = evaluate_kosis_recommendations(
        gold_eval, recommendations, config
    )
    return {
        "hcx_cache": hcx_cache,
        "recommendations": recommendations,
        "metrics": metrics,
        "detail": detail,
        "kosis_top_k_detail": kosis_top_k_detail,
    }


def main():
    """설정과 평가 데이터를 준비한 뒤 전체 few-shot 파이프라인을 실행합니다."""
    root = find_project_root()
    config = build_config(root)
    print({
        "HCX key loaded": bool(config.hcx_api_key),
        "KOSIS key loaded": bool(config.kosis_api_key),
        "model": config.hcx_url.rsplit("/", 1)[-1],
        "dry_run": config.dry_run,
    })
    target_articles, gold = load_evaluation_data(config)
    return run_pipeline(target_articles, gold, config)


if __name__ == "__main__":
    main()



