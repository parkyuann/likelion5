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
ARTICLE_FILENAME = "AI_기반_뉴스_사실검증_시스템_프로젝트_데이터_본문전처리.csv"
GOLD_FILENAME = "candidate_labeling_pilot_relaxed_team2_codex_prelabel.csv"


@dataclass(frozen=True)
class ExtractorConfig:
    """LLM 주장 추출에 필요한 경로, API, 처리량 설정을 보관합니다."""

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


def load_evaluation_data(config: ExtractorConfig):
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

def hcx_chat(messages, config: ExtractorConfig, session: requests.Session, retries=4):
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

def run_hcx(rows, config: ExtractorConfig):
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


def evaluate_claims(gold, hcx_cache, config: ExtractorConfig):
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
    return metrics, detail

def run_extraction(target_articles, gold, config: ExtractorConfig):
    """HCX 주장 추출과 gold 기반 추출 성능 평가를 실행합니다."""
    if target_articles.empty:
        raise ValueError("평가할 기사가 없습니다.")

    print(build_messages(target_articles.iloc[0])[-1]["content"][:500])
    if config.dry_run:
        print("DRY_RUN=True: API를 호출하지 않고 기존 캐시만 사용합니다.")
        hcx_cache = load_cache(config.cache_path)
    else:
        hcx_cache = run_hcx(target_articles, config)

    metrics, detail = evaluate_claims(gold, hcx_cache, config)
    return {"hcx_cache": hcx_cache, "metrics": metrics, "detail": detail}


def main():
    """설정과 평가 데이터를 준비한 뒤 LLM 주장 추출 단계를 실행합니다."""
    root = find_project_root()
    config = build_config(root)
    print({
        "HCX key loaded": bool(config.hcx_api_key),
        "model": config.hcx_url.rsplit("/", 1)[-1],
        "dry_run": config.dry_run,
    })
    target_articles, gold = load_evaluation_data(config)
    return run_extraction(target_articles, gold, config)


if __name__ == "__main__":
    main()
