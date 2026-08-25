# -*- coding: utf-8 -*-
"""질의 분류 — 통계 질의 vs 범위 밖.

자연어 질의가 KOSIS 공식 통계로 답할 수 있는 것인지 판별한다. 통계 질의만
MCP 사다리로 보내고, 전망·추천·의견 등 범위 밖 질의는 MCP를 호출하지 않고
안내로 종료한다(불필요한 도구 호출·비용 방지).
"""
from __future__ import annotations
import os
import re
import json
import uuid
import requests

_RAG_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/rag-reasoning"
_KEY = os.environ.get("HCX_API_KEY")

# 범위 밖 신호(전망·추천·주관) — 통계 조회로 답할 수 없는 질의
_OUT_OF_SCOPE = re.compile(r"할까요?$|될까요?$|좋을까|추천|어떻게 생각|전망|예측|의견|사는 게|팔아")


def classify(query: str) -> str:
    """'route_kosis_stat'(통계) 또는 'route_out_of_scope'(범위 밖).

    1차로 경량 규칙, 필요 시 RAG Reasoning으로 판별한다. 규칙만으로도
    다수를 거르며, 애매한 경우에만 모델 호출로 넘긴다.
    """
    if _OUT_OF_SCOPE.search(query.strip()):
        return "route_out_of_scope"
    if re.search(r"몇|얼마|추이|증감|비율|건수|인구|물가|고용|수출|GDP|출생|사망|소득|\d", query):
        return "route_kosis_stat"
    return _classify_by_model(query)


def _classify_by_model(query: str) -> str:
    try:
        r = requests.post(_RAG_URL,
                          headers={"Authorization": f"Bearer {_KEY}",
                                   "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
                                   "Content-Type": "application/json"},
                          json={"query": query,
                                "instruction": "이 질의가 공식 통계 수치로 답할 수 있는 것이면 route_kosis_stat, "
                                               "전망·추천·의견이면 route_out_of_scope 로만 답하라."},
                          timeout=60)
        txt = json.dumps(r.json(), ensure_ascii=False)
        return "route_out_of_scope" if "out_of_scope" in txt else "route_kosis_stat"
    except Exception:
        return "route_kosis_stat"  # 실패 시 보수적으로 통계 경로(사다리에서 재판별)
