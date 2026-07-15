from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from IPython.display import display


DRY_RUN = False
MAX_ARTICLES = None
# claim_extractor.py와 동일한 원본 기사 파일 — 본문_정제(전체 기사 텍스트)를 HCX 프롬프트에 맥락으로 넣는 데 쓴다.
RAW_ARTICLE_FILENAME = "news_preprocessed.csv"
# claim_extractor.py의 결과 파일 — 여기서 뽑힌 문장 후보를 그대로 LLM 판정 대상으로 쓴다
# (LLM이 원문에서 새로 추출하지 않고, 이미 regex로 걸러진 후보만 심사하는 2단계 파이프라인).
CLAIM_LISTFORM_FILENAME = "claim_listform.csv"
GOLD_FILENAME = "Labeled_test_data.csv"


@dataclass(frozen=True)
class ExtractorConfig:
    """LLM 주장 판정에 필요한 경로, API, 처리량 설정을 보관합니다."""

    data_dir: Path
    cache_path: Path
    result_path: Path
    hcx_api_key: str
    hcx_url: str
    dry_run: bool = True
    max_articles: int | None = None


def find_project_root() -> Path:
    """현재 파일 위치를 기준으로 data 폴더가 있는 프로젝트 루트를 찾습니다."""
    root = Path(__file__).resolve().parents[1]
    if not (root / "data").is_dir():
        raise FileNotFoundError(f"데이터 폴더를 찾을 수 없습니다: {root / 'data'}")
    return root


def build_config(root: Path) -> ExtractorConfig:
    """프로젝트 경로와 환경변수로 LLM 추출 설정을 생성합니다."""
    data_dir = root / "data"
    output_dir = root / "output" / "fewshot"
    output_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(root / ".env")
    hcx_model = os.getenv("HCX_MODEL", "HCX-005")
    return ExtractorConfig(
        data_dir=data_dir,
        cache_path=output_dir / "hcx_claim_cache.jsonl",
        result_path=output_dir / "fewshot_hcx_predictions.csv",
        hcx_api_key=os.getenv("NCP_CLOVASTUDIO_API_KEY", "").strip(),
        hcx_url=(
            "https://clovastudio.stream.ntruss.com/"
            f"v3/chat-completions/{hcx_model}"
        ),
        dry_run=DRY_RUN,
        max_articles=MAX_ARTICLES,
    )


CANDIDATE_COLUMNS = [
    "article_idx", "claim_text", "value_list", "unit_list",
    "time_ref", "time_compare", "change_type", "source_org_raw",
]


def load_evaluation_data(config: ExtractorConfig):
    """claim_extractor.py 결과(claim_listform.csv)에서 후보 문장을 가져와 평가 대상을 고른다.

    이 스크립트는 원문에서 처음부터 다시 추출하지 않는다 — claim_extractor.py가 regex로 이미
    걸러낸 후보 문장(claim_text)에 대해서만 LLM이 집계통계 여부/성격을 판정한다. gold 샘플의
    article_idx가 claim_extractor.py 결과에 실제로 존재하는지도 검증해, 이 단계가
    claim_extractor.py 다음 단계로 온전히 이어지도록 한다.
    """
    articles = pd.read_csv(
        config.data_dir / RAW_ARTICLE_FILENAME, encoding="utf-8-sig"
    )
    articles = articles.reset_index().rename(columns={"index": "article_idx"})
    articles["article_idx"] = articles["article_idx"].astype(int)

    claim_listform = pd.read_csv(
        config.data_dir / CLAIM_LISTFORM_FILENAME, encoding="utf-8-sig"
    )
    claim_listform["article_idx"] = claim_listform["article_idx"].astype(int)
    extracted_ids = set(claim_listform["article_idx"])

    gold = pd.read_csv(config.data_dir / "labeling" / GOLD_FILENAME, encoding="utf-8-sig")
    gold["article_idx"] = pd.to_numeric(gold["article_idx"], errors="raise").astype(int)

    target_ids = gold["article_idx"].drop_duplicates()
    missing_from_extractor = sorted(set(target_ids) - extracted_ids)
    if missing_from_extractor:
        raise ValueError(
            f"claim_extractor.py 결과({CLAIM_LISTFORM_FILENAME})에 없는 article_idx: "
            f"{missing_from_extractor} — claim_extractor.py를 먼저 실행했는지 확인하세요."
        )

    target_articles = articles[articles["article_idx"].isin(target_ids)].copy()
    target_articles = target_articles.sort_values("article_idx").reset_index(drop=True)
    missing_ids = sorted(set(target_ids) - set(target_articles["article_idx"]))
    if missing_ids:
        raise ValueError(f"본문 파일에서 article_idx를 찾지 못함: {missing_ids}")
    if not target_articles["본문_정제"].notna().all():
        raise ValueError("본문_정제 결측치가 있습니다.")

    # claim_extractor.py의 나열형 후처리(list-form split)는 같은 문장(claim_text)을 값 개수만큼
    # 여러 행으로 늘린다 — LLM 판정은 문장 단위로만 하면 되므로 (article_idx, claim_text) 기준
    # 중복을 제거해 후보당 한 번만 호출한다. 원본 CSV의 등장 순서(article_idx는 이미 정렬됨, 문장은
    # 기사 내 등장 순서)를 그대로 유지해 LLM이 맥락을 따라가기 쉽게 한다.
    candidates = (
        claim_listform[claim_listform["article_idx"].isin(target_ids)][CANDIDATE_COLUMNS]
        .drop_duplicates(subset=["article_idx", "claim_text"], keep="first")
        .reset_index(drop=True)
    )
    candidates["candidate_id"] = candidates.groupby("article_idx").cumcount()

    print(
        f"claim_extractor 결과 기사: {len(extracted_ids):,} / gold 행: {len(gold):,} "
        f"/ 평가 기사: {len(target_articles):,} (전체 {len(articles):,}) "
        f"/ 판정 대상 후보 문장: {len(candidates):,}"
    )
    display(
        target_articles[["article_idx", "기사제목", "작성일", "본문_길이"]].head()
    )
    return target_articles, candidates, gold


SYSTEM_PROMPT = r'''당신은 한국 뉴스 문장 후보(candidates)를 심사해 검증 가능한 집계통계 주장인지 판정하는 데이터 분석가다.
각 후보 문장은 규칙 기반 시스템(claim_extractor.py)이 기사에서 이미 뽑아낸 것이며, value/unit/time_ref 등은
참고용 힌트로 주어진다. 후보 문장 자체나 value/unit을 새로 만들거나 고치지 마라 — 각 후보에 대한 판정만 추가한다.

[claim_class 분류]
- 집계통계: 인구/고용/물가/무역/복지/교육 등 집단·기간을 집계한 관측 통계. KOSIS 등 공식 통계표로 확인 가능.
- 개별사례: 개별 기업·개인·사건 1건에 대한 수치(매출/투자/주가/지분 등).
- 법령제도: 법령·제도가 정한 기준값(세율, 최저임금, 벌금 등).
- 전망예측: 미래 전망·예측치.
- 목표계획: 정책·기업의 목표·계획 수치(아직 실현되지 않음).
- 노이즈: 광고·재생목록·UI 텍스트 등 추출 오류로 섞여든 비문장.
- 해석수사: 수치 없이 수사적으로 비교·강조하는 서술(정확한 통계값이 아님).
- 통계조사안내: 통계조사의 실시·발표 일정 등 조사 자체에 대한 안내(조사 결과값이 아님).

is_aggregate_claim은 claim_class가 "집계통계"일 때만 true로 한다.

반드시 JSON 객체 하나만 출력한다. 설명이나 코드펜스를 쓰지 않는다. candidates 배열의 candidate_id 각각에 대해
정확히 하나씩, 빠짐없이 같은 개수로 결과를 채워라(임의로 후보를 추가하거나 누락하지 마라).
스키마:
{"results":[{
 "candidate_id": 0,
 "is_aggregate_claim": true,
 "claim_class": "집계통계|개별사례|법령제도|전망예측|목표계획|노이즈|해석수사|통계조사안내",
 "indicator": "통계 지표명 또는 빈 문자열",
 "population": "대상 집단/지역 또는 빈 문자열",
 "source_org_mentioned": "기사에 명시된 기관 또는 빈 문자열",
 "expected_kosis_org": "KOSIS에서 예상되는 작성기관 또는 빈 문자열",
 "kosis_search_keywords": ["짧고 구체적인 검색어"],
 "reason": "판정 근거"
}]}
기관이나 통계표 ID를 지어내지 않는다.'''

FEW_SHOT = [
    (
        {
            "title": "취업자 증가",
            "date": "2025-01-15",
            "text": "통계청에 따르면 지난달 취업자는 2,900만명으로 전년 동월보다 18만명 늘었다. A사는 내년까지 4천억원을 투자해 생산량을 30% 늘릴 계획이다.",
            "candidates": [
                {"candidate_id": 0, "claim_text": "통계청에 따르면 지난달 취업자는 2,900만명으로 전년 동월보다 18만명 늘었다.", "value": "2900만;18만", "unit": "명;명", "time_ref": "지난달", "time_compare": "전년 동월", "change_type": "증감률", "source_org_raw": "통계청"},
                {"candidate_id": 1, "claim_text": "A사는 내년까지 4천억원을 투자해 생산량을 30% 늘릴 계획이다.", "value": "4000억;30", "unit": "원;%", "time_ref": "", "time_compare": "", "change_type": "단순수치", "source_org_raw": ""},
            ],
        },
        {
            "results": [
                {"candidate_id": 0, "is_aggregate_claim": True, "claim_class": "집계통계", "indicator": "취업자 수", "population": "전국 취업자", "source_org_mentioned": "통계청", "expected_kosis_org": "통계청", "kosis_search_keywords": ["취업자 수", "고용동향 취업자"], "reason": "전국 고용 집계의 시계열 수치"},
                {"candidate_id": 1, "is_aggregate_claim": False, "claim_class": "목표계획", "indicator": "", "population": "", "source_org_mentioned": "", "expected_kosis_org": "", "kosis_search_keywords": [], "reason": "개별 기업의 미래 투자·생산 계획"},
            ]
        },
    ),
    (
        {
            "title": "로또 당첨 안내",
            "date": "2025-07-12",
            "text": "1180회 로또 1등 11명당첨번호나 순위로 25억씩 배당. Current Time 0:00 / Duration 1:37 Loaded : 4.12% 0:00 Stream Type LIVE",
            "candidates": [
                {"candidate_id": 0, "claim_text": "Current Time 0:00 / Duration 1:37 Loaded : 4.12% 0:00 Stream Type LIVE", "value": "4.12", "unit": "%", "time_ref": "", "time_compare": "", "change_type": "증감률", "source_org_raw": ""},
            ],
        },
        {
            "results": [
                {"candidate_id": 0, "is_aggregate_claim": False, "claim_class": "노이즈", "indicator": "", "population": "", "source_org_mentioned": "", "expected_kosis_org": "", "kosis_search_keywords": [], "reason": "동영상 플레이어 로딩 진행률 표시일 뿐 통계 주장이 아님"},
            ]
        },
    ),
    (
        {
            "title": "소비자물가",
            "date": "2025-02-01",
            "text": "지난해 소비자물가는 1년 전보다 2.3% 올랐고 농축수산물은 5.9% 상승했다. 정부는 내년 소비자물가 상승률을 2% 안팎으로 관리하겠다고 밝혔다.",
            "candidates": [
                {"candidate_id": 0, "claim_text": "지난해 소비자물가는 1년 전보다 2.3% 올랐고 농축수산물은 5.9% 상승했다.", "value": "2.3;5.9", "unit": "%;%", "time_ref": "지난해", "time_compare": "1년 전", "change_type": "증감률", "source_org_raw": ""},
                {"candidate_id": 1, "claim_text": "정부는 내년 소비자물가 상승률을 2% 안팎으로 관리하겠다고 밝혔다.", "value": "2", "unit": "%", "time_ref": "내년", "time_compare": "", "change_type": "증감률", "source_org_raw": ""},
            ],
        },
        {
            "results": [
                {"candidate_id": 0, "is_aggregate_claim": True, "claim_class": "집계통계", "indicator": "소비자물가지수 상승률", "population": "전국", "source_org_mentioned": "", "expected_kosis_org": "통계청", "kosis_search_keywords": ["소비자물가지수", "농축수산물 소비자물가"], "reason": "공식 물가 집계로 확인 가능한 증감률"},
                {"candidate_id": 1, "is_aggregate_claim": False, "claim_class": "목표계획", "indicator": "", "population": "", "source_org_mentioned": "", "expected_kosis_org": "", "kosis_search_keywords": [], "reason": "미래 정책 목표치이며 실측 통계가 아님"},
            ]
        },
    ),
]


def candidates_payload(article_idx: int, candidates_df: pd.DataFrame) -> list[dict]:
    """한 기사의 claim_extractor.py 후보들을 HCX 프롬프트용 dict 목록으로 만든다."""
    sub = candidates_df[candidates_df["article_idx"] == article_idx]
    return [
        {
            "candidate_id": int(r.candidate_id),
            "claim_text": r.claim_text,
            "value": r.value_list,
            "unit": r.unit_list,
            "time_ref": r.time_ref if pd.notna(r.time_ref) else "",
            "time_compare": r.time_compare if pd.notna(r.time_compare) else "",
            "change_type": r.change_type,
            "source_org_raw": r.source_org_raw if pd.notna(r.source_org_raw) else "",
        }
        for r in sub.itertuples(index=False)
    ]


def build_messages(article_row, candidates: list[dict]):
    """기사 한 건 + 그 후보 문장 목록 + few-shot 예시를 HCX 대화 메시지로 구성합니다."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for x, y in FEW_SHOT:
        messages += [
            {"role": "user", "content": json.dumps(x, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(y, ensure_ascii=False)},
        ]
    payload = {
        "article_idx": int(article_row.article_idx),
        "title": article_row.기사제목,
        "date": str(article_row.작성일),
        "text": article_row.본문_정제,
        "candidates": candidates,
    }
    messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False)})
    return messages


def extract_json_object(text: str) -> dict:
    """모델 응답에서 JSON 객체를 추출하고 흔한 형식 오류를 보정합니다."""
    text = text.strip().lstrip('﻿')
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


def make_validator(expected_candidate_ids: list[int]):
    """candidates에 준 candidate_id가 결과에 빠짐없이, 정확히 한 번씩만 있는지 검증하는 함수를 만든다."""
    expected = set(expected_candidate_ids)

    def _validate(obj: dict) -> dict:
        if not isinstance(obj, dict) or not isinstance(obj.get("results"), list):
            raise ValueError("응답에 results 배열이 없습니다.")
        seen = set()
        for item in obj["results"]:
            if "candidate_id" not in item:
                raise ValueError("candidate_id가 없는 결과가 있습니다.")
            cid = int(item["candidate_id"])
            if cid in seen:
                raise ValueError(f"candidate_id={cid} 중복 응답")
            item["candidate_id"] = cid
            item.setdefault("is_aggregate_claim", False)
            item.setdefault("claim_class", "")
            item.setdefault("kosis_search_keywords", [])
            seen.add(cid)
        missing = expected - seen
        if missing:
            raise ValueError(f"candidate_id 누락: {sorted(missing)}")
        return obj

    return _validate


def hcx_chat(messages, config: ExtractorConfig, session: requests.Session, validate, retries=4):
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
            return validate(parsed), usage
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

def run_hcx(target_articles, candidates_df: pd.DataFrame, config: ExtractorConfig):
    """캐시에 없는 기사만, claim_extractor.py 후보 목록과 함께 HCX로 판정시키고 결과를 누적합니다."""
    cache = load_cache(config.cache_path)
    work = target_articles.head(config.max_articles) if config.max_articles else target_articles
    with requests.Session() as session:
        for i, row in enumerate(work.itertuples(index=False), 1):
            idx = int(row.article_idx)
            if idx in cache:
                continue
            candidates = candidates_payload(idx, candidates_df)
            if not candidates:
                item = {"article_idx": idx, "result": {"results": []}, "usage": {}}
                append_cache(item, config.cache_path)
                cache[idx] = item
                continue
            messages = build_messages(row, candidates)
            validate = make_validator([c["candidate_id"] for c in candidates])
            result, usage = hcx_chat(messages, config, session, validate)
            item = {"article_idx": idx, "result": result, "usage": usage}
            append_cache(item, config.cache_path)
            cache[idx] = item
            n_positive = sum(1 for r in result["results"] if r.get("is_aggregate_claim"))
            print(f"[{i}/{len(work)}] article_idx={idx}, candidates={len(candidates)}, 집계통계={n_positive}")
    return cache


PRED_COLUMNS = [
    "article_idx", "claim_text", "pred_is_aggregate_claim", "pred_claim_class",
    "pred_indicator", "pred_population", "pred_source_org_mentioned",
    "pred_expected_kosis_org", "pred_kosis_search_keywords", "pred_reason",
]


def flatten_predictions(cache, candidates_df: pd.DataFrame) -> pd.DataFrame:
    """기사별 캐시의 candidate 판정 결과를 (article_idx, claim_text) 키의 행 단위로 펼친다."""
    lookup = candidates_df.set_index(["article_idx", "candidate_id"])["claim_text"]
    rows = []
    for article_idx, item in cache.items():
        for result in item["result"]["results"]:
            key = (article_idx, result["candidate_id"])
            claim_text = lookup.get(key)
            if claim_text is None:
                continue
            rows.append({
                "article_idx": article_idx,
                "claim_text": claim_text,
                "pred_is_aggregate_claim": bool(result.get("is_aggregate_claim", False)),
                "pred_claim_class": result.get("claim_class", ""),
                "pred_indicator": result.get("indicator", ""),
                "pred_population": result.get("population", ""),
                "pred_source_org_mentioned": result.get("source_org_mentioned", ""),
                "pred_expected_kosis_org": result.get("expected_kosis_org", ""),
                "pred_kosis_search_keywords": ";".join(result.get("kosis_search_keywords") or []),
                "pred_reason": result.get("reason", ""),
            })
    return pd.DataFrame(rows, columns=PRED_COLUMNS)


def evaluate_claims(gold, candidates_df: pd.DataFrame, hcx_cache, config: ExtractorConfig):
    """claim_extractor.py 후보에 대한 LLM 판정을 gold와 (article_idx, claim_text) 정확 매칭으로 비교한다.

    gold의 claim_text는 claim_extractor.py가 뽑은 문장 그대로이므로(둘 다 같은 regex 추출 결과),
    fuzzy 텍스트 유사도 매칭이 필요 없다 — 키가 정확히 일치한다.
    """
    pred = flatten_predictions(hcx_cache, candidates_df)
    print(f"판정 결과 {len(pred):,}건 (후보 {len(candidates_df):,}건 중)")

    gold_eval = gold.copy()
    gold_eval["gold_bool"] = gold_eval["gold_is_aggregate_claim"].map(
        lambda value: str(value).strip().lower() in {"true", "1", "yes", "y"}
    )
    detail = gold_eval.merge(pred, on=["article_idx", "claim_text"], how="left")
    detail["matched"] = detail["pred_is_aggregate_claim"].notna()
    detail["pred_is_aggregate_claim"] = detail["pred_is_aggregate_claim"].fillna(False)

    gold_bool = detail["gold_bool"]
    pred_bool = detail["pred_is_aggregate_claim"]
    tp = int((gold_bool & pred_bool).sum())
    fp = int((~gold_bool & pred_bool).sum())
    fn = int((gold_bool & ~pred_bool).sum())
    tn = int((~gold_bool & ~pred_bool).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (gold_bool == pred_bool).mean()

    matched_positive = detail[detail["matched"] & gold_bool & pred_bool]
    claim_class_accuracy = (
        (matched_positive["gold_claim_class"] == matched_positive["pred_claim_class"]).mean()
        if len(matched_positive) else float("nan")
    )

    metrics = pd.DataFrame([{
        "gold_positive": int(gold_bool.sum()), "matched": int(detail["matched"].sum()),
        "unmatched_gold": int((~detail["matched"]).sum()),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "candidate_level_accuracy": accuracy,
        "claim_class_accuracy_on_tp": claim_class_accuracy,
    }])
    display(metrics.round(4))

    detail.to_csv(config.result_path, index=False, encoding="utf-8-sig")
    print("상세 비교:", config.result_path)
    return metrics, detail

def run_extraction(target_articles, candidates_df, gold, config: ExtractorConfig):
    """claim_extractor.py 후보에 대한 HCX 판정과 gold 기반 평가를 실행합니다."""
    if target_articles.empty:
        raise ValueError("평가할 기사가 없습니다.")

    sample_candidates = candidates_payload(int(target_articles.iloc[0].article_idx), candidates_df)
    print(build_messages(target_articles.iloc[0], sample_candidates)[-1]["content"][:500])
    if config.dry_run:
        print("DRY_RUN=True: API를 호출하지 않고 기존 캐시만 사용합니다.")
        hcx_cache = load_cache(config.cache_path)
    else:
        hcx_cache = run_hcx(target_articles, candidates_df, config)

    metrics, detail = evaluate_claims(gold, candidates_df, hcx_cache, config)
    return {"hcx_cache": hcx_cache, "metrics": metrics, "detail": detail}


def main():
    """설정과 평가 데이터를 준비한 뒤 LLM 주장 판정 단계를 실행합니다."""
    root = find_project_root()
    config = build_config(root)
    print({
        "HCX key loaded": bool(config.hcx_api_key),
        "model": config.hcx_url.rsplit("/", 1)[-1],
        "dry_run": config.dry_run,
    })
    target_articles, candidates_df, gold = load_evaluation_data(config)
    return run_extraction(target_articles, candidates_df, gold, config)


if __name__ == "__main__":
    main()
