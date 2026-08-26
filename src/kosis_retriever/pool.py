# -*- coding: utf-8 -*-
"""후보 pool 합집합 — 검색 결과에 통계군 형제표를 더해 후보 목록을 만든다.

검색 실패의 대부분은 "통계를 못 찾는 것"이 아니라 "같은 통계군의 세부표가
상위 밖에 있는 것"이므로, 검색 상위에 형제표(같은 stat_id)를 합집합으로 더한다.
기존 후보를 하나도 빼지 않고 추가만 하므로 이전에 맞던 케이스가 새로 틀리지 않는다.
"""
from __future__ import annotations

from config import POOL_LIVE_TOPK


def build_pool(search_hits: list[str], sid_of, siblings_of, limit: int = 30) -> list[str]:
    """검색 상위 + 통계군 형제표 합집합.

    search_hits: 검색기 상위 표 키(순위순).
    sid_of(table_key)->stat_id, siblings_of(stat_id)->[table_key...]: 카탈로그 조회.
    """
    pool = list(dict.fromkeys(search_hits))
    seen_sid = set()
    extra = []
    for tk in pool[:POOL_LIVE_TOPK]:
        sid = sid_of(tk)
        if not sid or sid in seen_sid:
            continue
        seen_sid.add(sid)
        for sib in siblings_of(sid):
            if sib not in pool and sib not in extra:
                extra.append(sib)
    return (pool + extra)[:limit]


def union_pools(frozen_pool: list[str], live_hits: list[str], limit: int = 30) -> list[str]:
    """기존(냉동) 후보 목록에 라이브 검색 상위를 합집합으로 더한다(단조 증가).

    기존 목록에서 아무것도 빼지 않으므로 회귀가 발생하지 않는다.
    """
    pool = list(frozen_pool)
    for tk in live_hits[:POOL_LIVE_TOPK]:
        if tk not in pool:
            pool.append(tk)
    return pool[:limit]
