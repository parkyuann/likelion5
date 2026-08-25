# -*- coding: utf-8 -*-
"""kosis_retriever — 뉴스 주장 → KOSIS 통계표 검색기 (recall@50 92.8% 구성).

실험(CLAUDE_file/recall_experiments)에서 검증된 구성을 라이브 검색 함수로 정리한 것:
  BM25(b2·b4) + dense(doc_meta·tbl_name) + HyDE(G0 표이름 · G2 분류값형식)
  → 가중 RRF(k=60) → org 소프트부스트(×5) → top_k
"""
from .retriever import retrieve, close

__all__ = ["retrieve", "close"]
