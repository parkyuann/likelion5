# -*- coding: utf-8 -*-
"""kosis_retriever 진입점 — 뉴스 주장 → KOSIS 통계표 검색 (recall@50 92.8% 구성).

라이브로 6경로를 계산해 융합한다:
  BM25 b2·b4  +  dense(doc_meta·tbl_name)  +  HyDE G0·G2  → 가중 RRF → org 소프트부스트 → top_k

의존: Qdrant(kosis_tables_v5) 실행 · output/bm25_v5.pkl · .env(HCX_API_KEY).
사용:
    from kosis_retriever import retrieve
    hits = retrieve("한국은행 발표 경상수지 990억달러", top_k=50)
"""
from __future__ import annotations
import sys
import pickle
from pathlib import Path

# BM25 색인·토크나이저 — 검색단 내부 모듈로 자기완결.
# bm25_*.pkl은 과거 'search_hybrid_v2' 모듈 경로로 피클됐으므로 언피클용 별칭을 건다.
from . import bm25_backend as _bm25lib
sys.modules.setdefault("search_hybrid_v2", _bm25lib)
from .bm25_backend import DocMetaBM25, KiwiRepresentations  # noqa: E402,F401

from .config import QDRANT_URL, COLLECTION, BM25_PKL, PER_PATH_N, TOP_K_DEFAULT
from .embed import embed
from .org_infer import infer_orgs
from .hyde import hyde_g0, hyde_g2
from .fusion import fuse

# ── 무거운 자원은 1회 초기화 후 재사용 ──────────────
_bm25 = None
_kiwi = None
_client = None


def _init():
    global _bm25, _kiwi, _client
    if _bm25 is None:
        if not BM25_PKL.exists():
            raise FileNotFoundError(f"BM25 인덱스 없음: {BM25_PKL} (색인 먼저 필요)")
        with BM25_PKL.open("rb") as f:
            _bm25 = pickle.load(f)["bm25"]
        _kiwi = KiwiRepresentations()
        from qdrant_client import QdrantClient
        _client = QdrantClient(url=QDRANT_URL, timeout=120)


def _gk(h):
    return getattr(h, "table_key", None) or (h.get("table_key") if isinstance(h, dict) else None)


def _dense(vec, vector_name, limit):
    """dense 검색 — Qdrant 일시 끊김 대비 재시도+재연결(실험 dense와 동일 견고성)."""
    global _client
    if vec is None:
        return []
    import time
    from qdrant_client import QdrantClient
    for attempt in range(6):
        try:
            r = _client.query_points(COLLECTION, query=vec, using=vector_name, limit=limit,
                                     with_payload=["table_key"])
            return [str((p.payload or {}).get("table_key") or "") for p in r.points]
        except Exception:
            time.sleep(min(2 ** attempt, 8))
            try: _client.close()
            except Exception: pass
            _client = QdrantClient(url=QDRANT_URL, timeout=120)
    return []


def retrieve(claim: str, top_k: int = TOP_K_DEFAULT, per_path_n: int = PER_PATH_N):
    """주장 → 상위 table_key 리스트 [{'table_key','score'}]. (fusion 순위·점수)"""
    claim = str(claim or "").strip()
    if not claim:
        return []
    _init()

    # 1) BM25 b2·b4 (org 필터 없이 — org는 소프트부스트로 반영)
    b2_hits, _ = _bm25.search(claim, _kiwi.all_morphemes, per_path_n, None)
    b4_hits, _ = _bm25.search(claim, _kiwi.expanded_core, per_path_n, None)

    # 2) dense: 주장 임베딩 → doc_meta / tbl_name 벡터
    cvec = embed(claim)
    dense_docmeta = _dense(cvec, "doc_meta_vector", per_path_n)
    dense_tblname = _dense(cvec, "tbl_name_vector", per_path_n)

    # 3) HyDE G0(표이름)·G2(분류값형식) → 각 임베딩 → doc_meta 벡터
    g0_vec = embed(hyde_g0(claim))
    g2_vec = embed(hyde_g2(claim))
    hyde_g0_path = _dense(g0_vec, "doc_meta_vector", per_path_n)
    hyde_g2_path = _dense(g2_vec, "doc_meta_vector", per_path_n)

    paths = {
        "dense_docmeta": dense_docmeta,
        "dense_tblname": dense_tblname,
        "bm25_b2": [_gk(h) for h in b2_hits if _gk(h)],
        "bm25_b4": [_gk(h) for h in b4_hits if _gk(h)],
        "hyde_g0": hyde_g0_path,
        "hyde_g2": hyde_g2_path,
    }

    # 4) org 소프트부스트 + 융합
    orgs = infer_orgs(claim)
    ranked = fuse(paths, orgs)
    # 점수 재부여(표시용): fuse는 순위만 반환 → rank 기반 간이 점수
    return [{"table_key": k, "rank": i + 1} for i, k in enumerate(ranked[:top_k])]


def close():
    global _client
    if _client is not None:
        try: _client.close()
        except Exception: pass
        _client = None
