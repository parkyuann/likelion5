# -*- coding: utf-8 -*-
"""경로·상수 단일 출처(single source of truth).

카탈로그·색인·검증 임계값을 한 곳에서 관리한다. 데이터 파일을 교체할 때
아래 파일명 상수만 수정하면 전체 파이프라인이 따라간다.
경로는 이 파일 위치(HERE) 기준 상대 경로로만 지정한다 — 폴더째 옮겨도 동작한다.
"""
from __future__ import annotations
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent   # src
ROOT = HERE.parent                       # 프로젝트 루트(src의 상위)
DATA = ROOT / "KOSIS_reindex"            # 카탈로그·색인 보관 위치
INDEX = ROOT / "output"                  # 색인 산출물 위치

# ── 데이터 파일(교체 시 이 이름만 변경) ─────────────────────────────
CATALOG = DATA / "kosis_catalog_v5_260817.jsonl"   # 통계표 카탈로그(표명·항목·축·단위·주기)
EMBED_CACHE = DATA / "embed_cache_doc.jsonl"       # 문서 임베딩 캐시
BM25_INDEX = INDEX / "bm25_v5.pkl"                 # BM25 색인

# ── 외부 서비스 ────────────────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "kosis_tables_v5")
MCP_URL = os.getenv("KOSIS_MCP_URL", "https://kosismcp2026.vercel.app/api/mcp")

# ── 검증 임계값(값순회·판정) ───────────────────────────────────────
TOLERANCE_EXACT = 0.005          # 직접값·증감 일치 상대오차
TOLERANCE_YOY_PP = 0.15          # 증감률(%p) 일치 밴드
TOLERANCE_REVISION = 0.12        # 개정-추정 근사 상대오차
TOLERANCE_REVISION_YOY_PP = 0.5  # 개정-추정 증감률 밴드
SCALES = (1, 1e2, 1e3, 1e4, 1e6, 1e7, 1e8)  # 단위 배율(명↔천명↔만명·억원↔조원)

# ── 순회 예산 ──────────────────────────────────────────────────────
WALK_BUDGET = 30          # 값순회로 열어보는 최대 표 수
FAMILY_CAP = 6            # 선택 표의 형제 중 우선 순회 상한
POOL_LIVE_TOPK = 10       # pool 합집합에 추가하는 라이브 검색 상위 수
