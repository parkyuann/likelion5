"""하이브리드 검색 후보(hybrid_top20.jsonl)를 BGE 리랭커로 재정렬한다 (실전2 T2-4).

파이프라인 위치:
    기사 주장 → [벡터DB 하이브리드 검색] → 후보 20개(hybrid_top20.jsonl)
             → [이 스크립트: 리랭커] → 재정렬된 후보 → (이후 값 조회·검증)

벡터DB 검색은 빠르지만 순서가 거칠다. 리랭커(BGE-reranker-v2-m3, cross-encoder)는
(질의, 문서)를 함께 읽어 관련도를 정밀 채점하므로, 정답 표를 위로 끌어올린다.
질의는 claim_text, 문서는 후보의 tbl_name 을 쓴다(후보에 표명만 있으므로).

입력(hybrid_top20.jsonl) 한 줄 = claim 1개:
    {claim_id, claim_text, claim_dense_query, predicted_tbl_nm, final_candidates:[20개]}
    후보: {table_key, rank, fusion_score, tbl_name, path_ranks, path_scores}

출력(hybrid_top20_reranked.jsonl)은 입력과 같은 구조인데, final_candidates 를
리랭커 점수 내림차순으로 재정렬하고 각 후보에 아래 두 필드를 덧붙인다:
    rerank_score : BGE 관련도 점수(0~1, sigmoid 정규화. 높을수록 관련)
    rerank_rank  : 재정렬 후 순위(1부터). 원래 하이브리드 순위 rank/fusion_score 는 보존.

모델은 로컬에서 실행되며 무료다. 최초 1회 모델(약 2.3GB)을 다운로드한다.
리랭킹 로직은 rerank_candidates() 함수로 분리해, 나중에 kosis_retriever.py 에
통합하거나 다른 리랭커로 교체하기 쉽게 했다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/candidate_reranker.py
    venv/Scripts/python.exe src/candidate_reranker.py --limit 5   # 5개 claim만 테스트
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "hybrid_top20.jsonl"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"


def reranked_output_path(input_path: Path) -> Path:
    """출력 경로 = 입력 파일명 뒤에 _reranked 를 붙인 것.

    예: hybrid_top20.jsonl → hybrid_top20_reranked.jsonl (같은 폴더).
    """
    return input_path.with_name(f"{input_path.stem}_reranked{input_path.suffix}")


def read_claims(path: Path) -> list[dict]:
    """JSON 객체를 줄바꿈에 의존하지 않고 순차적으로 읽는다.

    hybrid_top20.jsonl 은 대체로 한 줄=한 claim 이지만 일부 레코드가 여러 줄에
    걸쳐 있어, 공백을 건너뛰며 raw_decode 로 객체 경계를 직접 찾는다.
    """
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    objs: list[dict] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        obj, i = decoder.raw_decode(text, i)
        objs.append(obj)
    return objs


def rerank_candidates(reranker, query: str, candidates: list[dict],
                      doc_field: str = "tbl_name") -> list[dict]:
    """후보들을 (query, 후보[doc_field]) 관련도로 재정렬해 반환한다.

    각 후보에 rerank_score(0~1)와 rerank_rank(1부터)를 덧붙인다. 원본 필드는 유지.
    후보가 비어 있으면 그대로 돌려준다.
    """
    if not candidates:
        return candidates

    pairs = [[query, str(c.get(doc_field) or "")] for c in candidates]
    # normalize=True → sigmoid 로 0~1 정규화(해석·비교 쉬움).
    scores = reranker.compute_score(pairs, normalize=True)
    if not isinstance(scores, list):  # 후보 1개면 스칼라로 올 수 있음
        scores = [scores]

    scored = []
    for cand, score in zip(candidates, scores):
        c = dict(cand)
        c["rerank_score"] = round(float(score), 6)
        scored.append(c)

    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    for new_rank, c in enumerate(scored, start=1):
        c["rerank_rank"] = new_rank
    return scored


def main() -> None:
    parser = argparse.ArgumentParser(description="하이브리드 후보 → BGE 리랭킹 (T2-4)")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=None,
                        help="미지정 시 입력명 뒤에 _reranked 를 붙여 자동 생성")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--query-field", type=str, default="claim_text",
                        help="claim에서 질의로 쓸 필드 (기본 claim_text)")
    parser.add_argument("--doc-field", type=str, default="tbl_name",
                        help="후보에서 문서로 쓸 필드 (기본 tbl_name)")
    parser.add_argument("--candidates-field", type=str, default="final_candidates")
    parser.add_argument("--limit", type=int, default=None, help="앞에서 N개 claim만 (테스트용)")
    parser.add_argument("--use-fp16", action="store_true",
                        help="GPU에서 fp16 가속 (CPU면 끄는 게 안전, 기본 꺼짐)")
    args = parser.parse_args()

    output_path = args.output if args.output is not None else reranked_output_path(args.input)

    claims = read_claims(args.input)
    if args.limit is not None:
        claims = claims[: args.limit]
    print(f"[LOAD] claim {len(claims)}개 | {args.input}", flush=True)

    # FlagEmbedding 은 무거워서(torch 등) 필요할 때 import 한다.
    from FlagEmbedding import FlagReranker

    print(f"[MODEL] {args.model} 로드 중… (최초 실행 시 다운로드)", flush=True)
    t0 = time.time()
    reranker = FlagReranker(args.model, use_fp16=args.use_fp16)
    print(f"[MODEL] 로드 완료 ({time.time() - t0:.1f}초)", flush=True)

    started = time.time()
    pair_count = 0
    with output_path.open("w", encoding="utf-8") as handle:
        for i, claim in enumerate(claims, start=1):
            query = str(claim.get(args.query_field) or "")
            candidates = claim.get(args.candidates_field, [])
            reranked = rerank_candidates(reranker, query, candidates, doc_field=args.doc_field)
            pair_count += len(candidates)

            out = dict(claim)
            out[args.candidates_field] = reranked
            handle.write(json.dumps(out, ensure_ascii=False) + "\n")

            if i % 20 == 0 or i == len(claims):
                print(f"[RERANK] {i}/{len(claims)} claim | {pair_count}쌍 채점 | "
                      f"경과 {time.time() - started:.1f}초", flush=True)

    print(f"\n=== 완료: claim {len(claims)}개, 후보쌍 {pair_count}개 재정렬 | "
          f"소요 {time.time() - started:.1f}초 | 출력 {output_path} ===", flush=True)


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
