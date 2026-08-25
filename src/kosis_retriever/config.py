# -*- coding: utf-8 -*-
"""kosis_retriever 상수 — 검증된 실험 설정값.
   ★이 값들을 바꾸면 recall이 달라집니다. 수정 금지(재현 기준)."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]              # 프로젝트 루트
BM25_PKL = REPO / "output" / "bm25_v5.pkl"
CACHE_DIR = Path(__file__).resolve().parent / "_cache"  # 런타임 내부 캐시(반복 쿼리 절약)

# ── 인프라 ─────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION = "kosis_tables_v5"
EMB_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
HCX_007 = "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-007"

# ── 검색 파라미터 (실험 E28 그대로) ─────────────────
PER_PATH_N = 500          # 경로별 검색 깊이(deep_paths와 동일)
TOP_K_DEFAULT = 50        # 반환 상위 K (@50 운영)
RRF_K = 60                # RRF 상수
ORG_BOOST = 5.0           # org 소프트부스트 배수

# 융합 가중치: docmeta×2 · 표이름×1 · bm25 b2/b4×1 · HyDE G0/G2×1
E28 = {"dense_docmeta": 2, "dense_tblname": 1, "bm25_b2": 1, "bm25_b4": 1}
HYDE_WEIGHTS = {"hyde_g0": 1, "hyde_g2": 1}

# ── 기관(org) 매핑·지표규칙 ──
ORG_MAP = {"101": {"101"}, "301": {"301"}, "134": {"134", "360"}, "110": {"110"}, "326": {"326"}}
INDICATOR_RULES = [
    (r"수출|수입|무역|관세", {"134", "360"}),
    (r"경상수지|국제수지|외환보유|국내총생산|GDP|생산자물가|수출입물가|통화|기준금리", {"301"}),
    (r"출생|사망|혼인|이혼|인구|고용|취업|실업|소비자물가|물가|가계|소득|서비스업|양곡|농림|어업", {"101"}),
    (r"국세|세수|재정", {"110"}),
    (r"건설|주택|국토|수주", {"326"}),
]
