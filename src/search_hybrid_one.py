"""Run four-path hybrid retrieval for one claim and print JSON to stdout."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_CODE = PROJECT_ROOT / "archive" / "260722" / "code"
DEFAULT_CATALOG = PROJECT_ROOT / "data" / "kosis_catalog_v1.jsonl"
LOCAL_QDRANT = PROJECT_ROOT / "output" / "kosis_qdrant"
DEFAULT_COLLECTION = "kosis_tables"
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
EMBEDDING_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
HYDE_PROMPT = (
    "당신은 KOSIS 통계표 검색어 생성기입니다. 입력된 통계 주장과 대응할 법한 "
    "가상의 KOSIS 통계표명 하나를 예측하세요. 실제 표의 존재를 단정하지 말고, "
    "반드시 JSON 객체 하나만 출력하세요. 형식: {\"predicted_tbl_nm\":\"...\"}"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim-text", required=True, help="검색할 기사 Claim 문장")
    parser.add_argument("--claim-id", default="cli-single-claim")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--catalog-limit", type=int)
    parser.add_argument("--per-path-n", type=int, default=20)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--hcx-model", default="HCX-007")
    parser.add_argument("--org-id", action="append", help="반복 지정 가능한 기관 ID 필터")
    parser.add_argument("--local", action="store_true", help="로컬 Qdrant를 사용")
    parser.add_argument(
        "--qdrant-path",
        type=Path,
        default=LOCAL_QDRANT,
        help="--local 사용 시 Qdrant 저장 경로",
    )
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--output", type=Path, help="UTF-8 JSON 결과 파일 경로")
    parser.add_argument(
        "--include-path-details",
        action="store_true",
        help="디버깅용 경로별 후보와 순위를 결과에 포함",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="호환성 옵션. 검색 결과는 항상 stdout에만 출력됨",
    )
    return parser.parse_args()


def _bm25_path_hits(rows: list[dict[str, Any]], path: str, path_hit_type):
    return [
        path_hit_type(
            table_key=str(row["table_key"]),
            path=path,
            rank=rank,
            raw_score=float(row["score"]),
            tbl_name=str(row.get("tbl_name") or "").strip() or None,
            payload={"org_id": row.get("org_id")},
        )
        for rank, row in enumerate(rows, start=1)
    ]


def _load_api_key() -> str:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    key = (os.getenv("HCX_API_KEY") or os.getenv("NCP_CLOVASTUDIO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("HCX_API_KEY 또는 NCP_CLOVASTUDIO_API_KEY가 필요합니다")
    return key


def _embed(text: str) -> list[float]:
    import requests

    response = requests.post(
        EMBEDDING_URL,
        headers={"Authorization": f"Bearer {_load_api_key()}", "Content-Type": "application/json"},
        json={"text": text},
        timeout=30,
    )
    data = response.json()
    vector = data.get("result", {}).get("embedding")
    if response.status_code != 200 or not isinstance(vector, list) or not vector:
        raise RuntimeError(f"Embedding API 오류 (status={response.status_code}): {data.get('status')}")
    return [float(value) for value in vector]


def _generate_hyde(claim_text: str, model: str) -> str:
    import requests

    normalized_model = model.strip().upper()
    version = "v1" if normalized_model in {"HCX-003", "HCX-DASH-001"} else "v3"
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": HYDE_PROMPT},
            {"role": "user", "content": f"통계 주장:\n{claim_text}"},
        ],
        "temperature": 0.1 if version == "v1" else 0.0,
        "topP": 0.8,
        "topK": 0,
        "repetitionPenalty": 1.1,
    }
    if version == "v1":
        body.update({"maxTokens": 100, "stop": []})
    else:
        body["maxCompletionTokens"] = 100
        if normalized_model == "HCX-007":
            body["responseFormat"] = {
                "type": "json",
                "schema": {
                    "type": "object",
                    "properties": {"predicted_tbl_nm": {"type": "string"}},
                    "required": ["predicted_tbl_nm"],
                },
            }
            body["thinking"] = {"effort": "none"}
    response = requests.post(
        f"https://clovastudio.stream.ntruss.com/{version}/chat-completions/{normalized_model}",
        headers={
            "Authorization": f"Bearer {_load_api_key()}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HCX HyDE API 오류 ({response.status_code}): {response.text[:500]}")
    result = response.json().get("result", response.json())
    content = result.get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = str(content).strip().replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("HCX 응답에 JSON 객체가 없습니다")
    predicted = str(json.loads(text[start : end + 1]).get("predicted_tbl_nm") or "").strip()
    if not predicted:
        raise ValueError("HCX 응답에 predicted_tbl_nm이 없습니다")
    return predicted


def _dense_search(
    client,
    query: str,
    vector_name: str,
    path: str,
    top_k: int,
    org_ids,
    path_hit_type,
    *,
    collection: str = DEFAULT_COLLECTION,
    embed_fn=_embed,
):
    query_filter = None
    if org_ids:
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        query_filter = Filter(must=[FieldCondition(key="org_id", match=MatchAny(any=org_ids))])
    response = client.query_points(
        collection_name=collection,
        query=embed_fn(query),
        using=vector_name,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    hits = []
    for point in response.points:
        payload = point.payload or {}
        table_key = str(payload.get("table_key") or "").strip()
        if table_key:
            hits.append(path_hit_type(
                table_key=table_key,
                path=path,
                rank=len(hits) + 1,
                raw_score=float(point.score),
                tbl_name=str(payload.get("tbl_name") or "").strip() or None,
                payload=payload,
            ))
    return hits


def main() -> None:
    args = parse_args()
    claim_text = str(args.claim_text or "").strip()
    if not claim_text:
        raise ValueError("--claim-text must not be empty")
    if args.per_path_n < 1 or args.top_n < 1 or args.rrf_k < 1:
        raise ValueError("per-path-n, top-n and rrf-k must be >= 1")
    if args.catalog_limit is not None and args.catalog_limit < 1:
        raise ValueError("catalog-limit must be >= 1")
    if not args.catalog.exists():
        raise FileNotFoundError(args.catalog)
    if args.local and not args.qdrant_path.exists():
        raise FileNotFoundError(args.qdrant_path)
    if not str(args.collection or "").strip():
        raise ValueError("--collection must not be empty")
    if not ARCHIVE_CODE.exists():
        raise FileNotFoundError(f"BM25 experiment code not found: {ARCHIVE_CODE}")

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    sys.path.insert(0, str(ARCHIVE_CODE))

    from bm25_b1_b4_experiment import KiwiRepresentations
    from bm25_morph_experiment import BM25InvertedIndex
    from retrieval_backend import PathHit, validate_hits
    from retrieval_fusion import reciprocal_rank_fusion
    from retrieval_query_builder import build_claim_dense_query

    started = time.perf_counter()
    print("[1/5] Kiwi BM25 인덱스를 구성합니다.", file=sys.stderr, flush=True)
    kiwi = KiwiRepresentations()
    bm25_index = BM25InvertedIndex.build(
        args.catalog,
        kiwi,
        catalog_limit=args.catalog_limit,
    )

    eligible_org_ids = set(args.org_id) if args.org_id else None
    print("[2/5] B2/B4 BM25를 검색합니다.", file=sys.stderr, flush=True)
    b2_rows, b2_tokens = bm25_index.search(
        claim_text,
        kiwi.all_morphemes,
        args.per_path_n,
        eligible_org_ids,
    )
    b4_rows, b4_tokens = bm25_index.search(
        claim_text,
        kiwi.expanded_core,
        args.per_path_n,
        eligible_org_ids,
    )
    paths: dict[str, list[PathHit]] = {
        "b2_morph_bm25": _bm25_path_hits(b2_rows, "b2_morph_bm25", PathHit),
        "b4_core_bm25": _bm25_path_hits(b4_rows, "b4_core_bm25", PathHit),
    }

    from qdrant_client import QdrantClient

    client = (
        QdrantClient(path=str(args.qdrant_path.resolve()))
        if args.local
        else QdrantClient(url=QDRANT_URL)
    )
    predicted_tbl_nm: str | None = None
    qdrant_points: int | None = None
    errors: dict[str, str] = {}
    try:
        qdrant_points = int(client.count(args.collection, exact=True).count)
        print("[3/5] Claim Dense를 검색합니다.", file=sys.stderr, flush=True)
        claim_query = build_claim_dense_query({"claim_text": claim_text})
        try:
            claim_hits = _dense_search(
                client,
                claim_query,
                "doc_meta_vector",
                "claim_dense",
                args.per_path_n,
                args.org_id,
                PathHit,
                collection=args.collection,
            )
            validate_hits(claim_hits, args.per_path_n)
            paths["claim_dense"] = claim_hits
        except Exception as exc:
            errors["claim_dense"] = f"{type(exc).__name__}: {exc}"
            paths["claim_dense"] = []

        print("[4/5] HCX 예상 표명과 HyDE Dense를 검색합니다.", file=sys.stderr, flush=True)
        try:
            predicted_tbl_nm = _generate_hyde(claim_text, args.hcx_model)
            hyde_hits = _dense_search(
                client,
                predicted_tbl_nm,
                "tbl_name_vector",
                "hyde_dense",
                args.per_path_n,
                args.org_id,
                PathHit,
                collection=args.collection,
            )
            validate_hits(hyde_hits, args.per_path_n)
            paths["hyde_dense"] = hyde_hits
        except Exception as exc:
            errors["hyde_dense"] = f"{type(exc).__name__}: {exc}"
            paths["hyde_dense"] = []
    finally:
        client.close()

    print("[5/5] RRF로 최종 순위를 계산합니다.", file=sys.stderr, flush=True)
    fused = reciprocal_rank_fusion(paths, top_n=args.top_n, rrf_k=args.rrf_k)
    selected_tables = [
        {
            "final_rank": candidate.rank,
            "table_key": candidate.table_key,
            "tbl_name": candidate.tbl_name,
            "fusion_score": candidate.fusion_score,
        }
        for candidate in fused
    ]
    result = {
        "claim_id": args.claim_id,
        "claim_text": claim_text,
        "predicted_tbl_nm": predicted_tbl_nm,
        "selection_method": "RRF(b2_morph_bm25,b4_core_bm25,claim_dense,hyde_dense)",
        "selected_tables": selected_tables,
        "search_scope": {
            "bm25_catalog_documents": len(bm25_index.documents),
            "dense_qdrant_points": qdrant_points,
            "collection": args.collection,
        },
        "errors": errors,
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }
    if args.include_path_details:
        result["debug"] = {
            "query_tokens": {"b2": b2_tokens, "b4": b4_tokens},
            "path_results": {
                path: [asdict(hit) for hit in hits]
                for path, hits in paths.items()
            },
            "fused_candidates_with_path_ranks": [asdict(candidate) for candidate in fused],
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"result_json={args.output.resolve()}", file=sys.stderr, flush=True)
    if args.stdout or not args.output:
        print(rendered, flush=True)


if __name__ == "__main__":
    main()
