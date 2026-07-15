"""문제지(Unlabeled_test_data.csv)를 few-shot LLM(HCX)으로 풀고 정답지(Labeled_test_data.csv)와 채점한다.

실행 흐름:
  1. configs/fewshot_config.json (또는 --config로 지정한 파일)에서 프롬프트·모델·하이퍼파라미터를 읽는다.
  2. 문제지의 각 문항(candidate)을 기사 단위로 묶어 HCX에 판정을 요청한다 (풀이 시간 측정).
  3. 정답지와 candidate_row_id로 채점해 성능 지표를 계산한다.
  4. 틀린 문항은 입력으로 준 데이터와 모델 판정을 함께 기록한다(오답 분석).
  5. 설정·풀이 시간·지표·오답을 output/fewshot/runs/<run_id>/ 아래 json/md로 남긴다.

사용 예 (레포 루트):
    venv/Scripts/python.exe src/fewshot_claim_extractor.py
    venv/Scripts/python.exe src/fewshot_claim_extractor.py --dry-run
    venv/Scripts/python.exe src/fewshot_claim_extractor.py --model HCX-005 --temperature 0.2 --max-articles 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv


DEFAULT_CONFIG_PATH = "configs/fewshot_config.json"

PROBLEM_INPUT_COLUMNS = [
    "claim_text", "value_list", "unit_list", "time_ref", "time_compare",
    "change_type", "source_org_raw",
]


def find_project_root() -> Path:
    """현재 파일 위치를 기준으로 data 폴더가 있는 프로젝트 루트를 찾습니다."""
    root = Path(__file__).resolve().parents[1]
    if not (root / "data").is_dir():
        raise FileNotFoundError(f"데이터 폴더를 찾을 수 없습니다: {root / 'data'}")
    return root


@dataclass
class ExtractorConfig:
    """configs/*.json + CLI 오버라이드로 채워지는 프롬프트·모델·경로 설정."""

    root: Path
    model: str
    hcx_url_template: str
    temperature: float
    top_p: float
    top_k: int
    max_tokens: int
    repetition_penalty: float
    retries: int
    system_prompt: str
    few_shot: list[dict]
    problem_path: Path
    answer_path: Path
    article_path: Path
    dry_run: bool
    max_articles: int | None
    hcx_api_key: str = field(default="", repr=False)
    run_id: str = ""

    @property
    def hcx_url(self) -> str:
        return self.hcx_url_template.format(model=self.model)

    def to_json_dict(self) -> dict:
        """API 키를 제외하고 실험 재현에 필요한 설정만 직렬화한다."""
        return {
            "run_id": self.run_id,
            "model": self.model,
            "hcx_url": self.hcx_url,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_tokens": self.max_tokens,
            "repetition_penalty": self.repetition_penalty,
            "retries": self.retries,
            "system_prompt": self.system_prompt,
            "few_shot": self.few_shot,
            "problem_path": str(self.problem_path),
            "answer_path": str(self.answer_path),
            "article_path": str(self.article_path),
            "dry_run": self.dry_run,
            "max_articles": self.max_articles,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="문제지를 HCX few-shot으로 풀고 정답지와 채점한다")
    parser.add_argument("--config", type=Path, default=None, help=f"설정 JSON 경로 (기본: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--run-id", type=str, default=None, help="결과 폴더 이름(기본: 실행 시각)")
    parser.add_argument("--dry-run", action="store_true", default=None, help="API 호출 없이 기존 캐시만으로 채점")
    parser.add_argument("--max-articles", type=int, default=None, help="처리할 기사 수 제한(스모크 테스트용)")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--repetition-penalty", type=float, default=None)
    return parser.parse_args(argv)


def build_config(root: Path, args: argparse.Namespace) -> ExtractorConfig:
    """설정 JSON을 읽고 CLI 인자로 오버라이드한 뒤 실행 설정을 만든다."""
    config_path = args.config or (root / DEFAULT_CONFIG_PATH)
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    load_dotenv(root / ".env")

    overrides = {
        "model": args.model,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "max_tokens": args.max_tokens,
        "repetition_penalty": args.repetition_penalty,
        "dry_run": args.dry_run,
        "max_articles": args.max_articles,
    }
    for key, value in overrides.items():
        if value is not None:
            raw[key] = value

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    return ExtractorConfig(
        root=root,
        model=raw["model"],
        hcx_url_template=raw["hcx_url_template"],
        temperature=raw["temperature"],
        top_p=raw["top_p"],
        top_k=raw["top_k"],
        max_tokens=raw["max_tokens"],
        repetition_penalty=raw["repetition_penalty"],
        retries=raw.get("retries", 4),
        system_prompt=raw["system_prompt"],
        few_shot=raw["few_shot"],
        problem_path=root / raw["problem_path"],
        answer_path=root / raw["answer_path"],
        article_path=root / raw["article_path"],
        dry_run=bool(raw.get("dry_run", False)),
        max_articles=raw.get("max_articles"),
        hcx_api_key=os.getenv("NCP_CLOVASTUDIO_API_KEY", "").strip(),
        run_id=run_id,
    )


def load_problem_data(config: ExtractorConfig):
    """문제지 + 기사 본문을 읽어, 기사 단위로 묶인 문항(candidate) 목록을 만든다."""
    problems = pd.read_csv(config.problem_path, encoding="utf-8-sig")
    problems["article_idx"] = problems["article_idx"].astype(int)
    missing_cols = set(PROBLEM_INPUT_COLUMNS + ["candidate_row_id", "article_idx"]) - set(problems.columns)
    if missing_cols:
        raise ValueError(f"문제지 필수 컬럼 누락: {sorted(missing_cols)}")

    articles = pd.read_csv(config.article_path, encoding="utf-8-sig")
    articles = articles.reset_index().rename(columns={"index": "article_idx"})
    articles["article_idx"] = articles["article_idx"].astype(int)
    body_lookup = articles.set_index("article_idx")["본문_정제"]

    problems = problems.copy()
    problems["본문_정제"] = problems["article_idx"].map(body_lookup)
    missing_body = problems["본문_정제"].isna().sum()
    if missing_body:
        print(f"경고: 기사 본문을 찾지 못한 문항 {missing_body}건 — 빈 본문으로 대체합니다.")
        problems["본문_정제"] = problems["본문_정제"].fillna("")

    # 문제지 안에서의 등장 순서(셔플된 순서 그대로)를 기사별 candidate_id로 쓴다.
    problems["candidate_id"] = problems.groupby("article_idx").cumcount()

    if config.max_articles:
        keep_ids = list(dict.fromkeys(problems["article_idx"]))[: config.max_articles]
        problems = problems[problems["article_idx"].isin(keep_ids)].reset_index(drop=True)

    n_articles = problems["article_idx"].nunique()
    print(f"문제지 문항: {len(problems):,}건 / 기사: {n_articles:,}건 (파일: {config.problem_path.name})")
    return problems


def candidates_payload(article_idx: int, problems: pd.DataFrame) -> list[dict]:
    """한 기사에 속한 문항들을 HCX 프롬프트용 dict 목록으로 만든다."""
    sub = problems[problems["article_idx"] == article_idx]
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


def build_messages(config: ExtractorConfig, article_idx: int, title: str, date: str, text: str, candidates: list[dict]):
    """설정에 담긴 시스템 프롬프트 + few-shot + 문항 목록으로 HCX 대화 메시지를 구성한다."""
    messages = [{"role": "system", "content": config.system_prompt}]
    for shot in config.few_shot:
        messages += [
            {"role": "user", "content": json.dumps(shot["user"], ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(shot["assistant"], ensure_ascii=False)},
        ]
    payload = {
        "article_idx": int(article_idx),
        "title": title,
        "date": str(date),
        "text": text,
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
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    candidate = ''.join(ch if ord(ch) >= 32 or ch in '\n\r\t' else ' ' for ch in candidate)
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError as error:
        context = candidate[max(0, error.pos - 80):error.pos + 80]
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


def hcx_chat(messages, config: ExtractorConfig, session: requests.Session, validate):
    """HCX를 호출하고 필요하면 깨진 JSON을 복구해 구조화 결과를 반환합니다."""
    if not config.hcx_api_key:
        raise RuntimeError(".env에 NCP_CLOVASTUDIO_API_KEY가 필요합니다.")
    headers = {
        "Authorization": f"Bearer {config.hcx_api_key}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "messages": messages, "temperature": config.temperature, "topP": config.top_p,
        "topK": config.top_k, "maxTokens": config.max_tokens,
        "repetitionPenalty": config.repetition_penalty, "stop": [],
    }
    for attempt in range(config.retries):
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
                repair_body = {**body, "messages": [
                    {"role": "system", "content": (
                        "입력 내용을 변경·요약하지 말고 유효한 JSON 객체 하나로만 재직렬화하라. "
                        "문자열 내부의 큰따옴표와 줄바꿈을 반드시 JSON 규칙에 맞게 escape하라. "
                        "코드펜스와 설명은 출력하지 마라."
                    )},
                    {"role": "user", "content": content},
                ], "maxTokens": max(config.max_tokens, 2000)}
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
            if attempt == config.retries - 1:
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


def solve_problems(problems: pd.DataFrame, config: ExtractorConfig, cache_path: Path):
    """문제지를 기사 단위로 풀이하고(캐시 재사용) 총 소요 시간을 함께 반환한다."""
    cache = load_cache(cache_path)
    article_order = list(dict.fromkeys(problems["article_idx"]))

    if config.dry_run:
        print(f"dry-run: API를 호출하지 않고 기존 캐시({len(cache)}건)만 사용합니다.")
        missing = [idx for idx in article_order if idx not in cache]
        if missing:
            print(f"경고: 캐시에 없는 기사 {len(missing)}건은 채점에서 제외됩니다: {missing[:10]}{' ...' if len(missing) > 10 else ''}")
        return cache, 0.0

    start = time.perf_counter()
    with requests.Session() as session:
        for i, article_idx in enumerate(article_order, 1):
            if article_idx in cache:
                continue
            sub = problems[problems["article_idx"] == article_idx]
            title = sub["기사제목"].iloc[0]
            date = sub["작성일"].iloc[0]
            text = sub["본문_정제"].iloc[0]
            candidates = candidates_payload(article_idx, problems)
            messages = build_messages(config, article_idx, title, date, text, candidates)
            validate = make_validator([c["candidate_id"] for c in candidates])
            result, usage = hcx_chat(messages, config, session, validate)
            item = {"article_idx": article_idx, "result": result, "usage": usage}
            append_cache(item, cache_path)
            cache[article_idx] = item
            n_positive = sum(1 for r in result["results"] if r.get("is_aggregate_claim"))
            print(f"[{i}/{len(article_order)}] article_idx={article_idx}, 문항={len(candidates)}, 집계통계={n_positive}")
    elapsed = time.perf_counter() - start
    return cache, elapsed


PRED_COLUMNS = [
    "article_idx", "candidate_id", "claim_text", "pred_is_aggregate_claim", "pred_claim_class",
    "pred_indicator", "pred_population", "pred_source_org_mentioned",
    "pred_expected_kosis_org", "pred_kosis_search_keywords", "pred_reason",
]


def flatten_predictions(cache, problems: pd.DataFrame) -> pd.DataFrame:
    """기사별 캐시의 candidate 판정 결과를 (article_idx, candidate_id) 키의 행 단위로 펼친다."""
    lookup = problems.set_index(["article_idx", "candidate_id"])["candidate_row_id"]
    claim_lookup = problems.set_index(["article_idx", "candidate_id"])["claim_text"]
    rows = []
    for article_idx, item in cache.items():
        for result in item["result"]["results"]:
            key = (article_idx, result["candidate_id"])
            if key not in lookup.index:
                continue
            rows.append({
                "article_idx": article_idx,
                "candidate_id": result["candidate_id"],
                "candidate_row_id": lookup.loc[key],
                "claim_text": claim_lookup.loc[key],
                "pred_is_aggregate_claim": bool(result.get("is_aggregate_claim", False)),
                "pred_claim_class": result.get("claim_class", ""),
                "pred_indicator": result.get("indicator", ""),
                "pred_population": result.get("population", ""),
                "pred_source_org_mentioned": result.get("source_org_mentioned", ""),
                "pred_expected_kosis_org": result.get("expected_kosis_org", ""),
                "pred_kosis_search_keywords": ";".join(result.get("kosis_search_keywords") or []),
                "pred_reason": result.get("reason", ""),
            })
    columns = ["candidate_row_id"] + PRED_COLUMNS
    return pd.DataFrame(rows, columns=columns)


def to_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def grade(problems: pd.DataFrame, pred: pd.DataFrame, config: ExtractorConfig):
    """정답지를 candidate_row_id로 채점하고, 지표 + 오답 상세를 함께 만든다."""
    answers = pd.read_csv(config.answer_path, encoding="utf-8-sig")
    answers["candidate_row_id"] = answers["candidate_row_id"].astype(int)
    answers["gold_bool"] = answers["gold_is_aggregate_claim"].map(to_bool)

    detail = answers.merge(pred, on="candidate_row_id", how="left", suffixes=("", "_pred"))
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
    accuracy = float((gold_bool == pred_bool).mean())

    matched_positive = detail[detail["matched"] & gold_bool & pred_bool]
    claim_class_accuracy = (
        float((matched_positive["gold_claim_class"] == matched_positive["pred_claim_class"]).mean())
        if len(matched_positive) else None
    )

    metrics = {
        "n_answers": int(len(answers)),
        "n_matched": int(detail["matched"].sum()),
        "n_unmatched": int((~detail["matched"]).sum()),
        "gold_positive": int(gold_bool.sum()),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "is_aggregate_claim_accuracy": round(accuracy, 4),
        "claim_class_accuracy_on_tp": round(claim_class_accuracy, 4) if claim_class_accuracy is not None else None,
    }

    # 오답 = 정답지에 매칭됐지만(모델이 응답했지만) is_aggregate_claim 또는(집계통계로 맞춘 경우) claim_class가 틀린 문항.
    wrong_mask = detail["matched"] & (
        (detail["gold_bool"] != detail["pred_is_aggregate_claim"])
        | (detail["gold_bool"] & (detail["gold_claim_class"] != detail["pred_claim_class"]))
    )
    input_cols = ["claim_text", "value_list", "unit_list", "time_ref", "time_compare",
                  "change_type", "source_org_raw", "기사제목", "작성일"]
    problem_inputs = problems.set_index("candidate_row_id")[input_cols]

    errors = []
    for row in detail.loc[wrong_mask].itertuples():
        row_id = row.candidate_row_id
        inputs = problem_inputs.loc[row_id].to_dict() if row_id in problem_inputs.index else {}
        errors.append({
            "candidate_row_id": int(row_id),
            "article_idx": int(row.article_idx),
            "mistake_type": (
                "is_aggregate_claim" if row.gold_bool != row.pred_is_aggregate_claim else "claim_class"
            ),
            "input_given_to_model": {k: (None if pd.isna(v) else v) for k, v in inputs.items()},
            "gold": {
                "is_aggregate_claim": bool(row.gold_bool),
                "claim_class": row.gold_claim_class,
                "indicator_raw": None if pd.isna(row.gold_indicator_raw) else row.gold_indicator_raw,
                "source_org_raw": None if pd.isna(row.gold_source_org_raw) else row.gold_source_org_raw,
            },
            "pred": {
                "is_aggregate_claim": bool(row.pred_is_aggregate_claim),
                "claim_class": row.pred_claim_class,
                "indicator": row.pred_indicator,
                "source_org_mentioned": row.pred_source_org_mentioned,
                "expected_kosis_org": row.pred_expected_kosis_org,
                "kosis_search_keywords": row.pred_kosis_search_keywords,
                "reason": row.pred_reason,
            },
        })

    unmatched = detail.loc[~detail["matched"], ["candidate_row_id", "article_idx", "claim_text"]]
    for row in unmatched.itertuples():
        errors.append({
            "candidate_row_id": int(row.candidate_row_id),
            "article_idx": int(row.article_idx),
            "mistake_type": "no_prediction",
            "input_given_to_model": None,
            "gold": None,
            "pred": None,
        })

    return metrics, errors, detail


def write_report(run_dir: Path, config: ExtractorConfig, elapsed_seconds: float, n_solved: int,
                  metrics: dict, errors: list[dict]):
    per_item = elapsed_seconds / n_solved if n_solved else 0.0
    lines = [
        f"# fewshot_claim_extractor 실행 결과 — {config.run_id}",
        "",
        "## 설정",
        f"- model: `{config.model}`",
        f"- temperature: {config.temperature}, top_p: {config.top_p}, top_k: {config.top_k}, "
        f"max_tokens: {config.max_tokens}, repetition_penalty: {config.repetition_penalty}",
        f"- dry_run: {config.dry_run}",
        f"- problem_path: `{config.problem_path.relative_to(config.root)}`",
        f"- answer_path: `{config.answer_path.relative_to(config.root)}`",
        "",
        "## 풀이 시간",
        f"- 총 소요: {elapsed_seconds:.1f}초 (새로 호출한 기사 기준)",
        f"- 기사당 평균: {per_item:.2f}초",
        "",
        "## 성능 지표",
        f"- 정답 문항 수: {metrics['n_answers']} / 매칭된 예측: {metrics['n_matched']} / 미매칭: {metrics['n_unmatched']}",
        f"- TP={metrics['tp']} FP={metrics['fp']} FN={metrics['fn']} TN={metrics['tn']}",
        f"- precision={metrics['precision']} recall={metrics['recall']} f1={metrics['f1']}",
        f"- is_aggregate_claim 정확도: {metrics['is_aggregate_claim_accuracy']}",
        f"- claim_class 정확도(TP 중): {metrics['claim_class_accuracy_on_tp']}",
        "",
        f"## 오답 ({len(errors)}건, 상세는 errors.json)",
    ]
    for err in errors[:20]:
        if err["mistake_type"] == "no_prediction":
            lines.append(f"- candidate_row_id={err['candidate_row_id']}: 예측 없음(캐시 미포함)")
            continue
        lines.append(
            f"- candidate_row_id={err['candidate_row_id']} [{err['mistake_type']}] "
            f"정답={err['gold']['is_aggregate_claim']}/{err['gold']['claim_class']} "
            f"→ 예측={err['pred']['is_aggregate_claim']}/{err['pred']['claim_class']} "
            f"(사유: {err['pred']['reason']})"
        )
    if len(errors) > 20:
        lines.append(f"- ... 외 {len(errors) - 20}건")

    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run(config: ExtractorConfig) -> dict:
    run_dir = config.root / "output" / "fewshot" / "runs" / config.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config_used.json").write_text(
        json.dumps(config.to_json_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    problems = load_problem_data(config)

    sample_candidates = candidates_payload(int(problems.iloc[0].article_idx), problems)
    sample_row = problems.iloc[0]
    preview = build_messages(
        config, sample_row.article_idx, sample_row.기사제목, sample_row.작성일,
        sample_row.본문_정제, sample_candidates,
    )[-1]["content"]
    print(preview[:500])

    cache_path = run_dir / "hcx_cache.jsonl"
    cache, elapsed_seconds = solve_problems(problems, config, cache_path)
    n_solved = sum(1 for r in problems["article_idx"].unique() if r in cache)
    print(f"풀이 완료: 기사 {n_solved}건, 소요 {elapsed_seconds:.1f}초")

    pred = flatten_predictions(cache, problems)
    pred.drop(columns=["candidate_row_id"]).to_csv(run_dir / "predictions.csv", index=False, encoding="utf-8-sig")

    metrics, errors, detail = grade(problems, pred, config)
    metrics["elapsed_seconds"] = round(elapsed_seconds, 2)
    metrics["n_articles_solved"] = n_solved
    metrics["seconds_per_article"] = round(elapsed_seconds / n_solved, 3) if n_solved else 0.0

    (run_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    detail.to_csv(run_dir / "graded_detail.csv", index=False, encoding="utf-8-sig")
    write_report(run_dir, config, elapsed_seconds, n_solved, metrics, errors)

    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"결과 저장 위치: {run_dir}")
    return {"config": config, "metrics": metrics, "errors": errors, "detail": detail, "run_dir": run_dir}


def main(argv: list[str] | None = None):
    args = parse_args(argv)
    root = find_project_root()
    config = build_config(root, args)
    print({
        "run_id": config.run_id,
        "model": config.model,
        "HCX key loaded": bool(config.hcx_api_key),
        "dry_run": config.dry_run,
    })
    return run(config)


if __name__ == "__main__":
    main()
