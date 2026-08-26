# -*- coding: utf-8 -*-
"""가중 RRF + org 소프트부스트.
   score += weight/(rrf_k+rank), 그 후 org 소속 표 점수 ×boost."""
from .config import E28, HYDE_WEIGHTS, RRF_K, ORG_BOOST


def fuse(paths, orgs, rrf_k=RRF_K, boost=ORG_BOOST):
    """paths: {경로명: [table_key 순위리스트]} · orgs: {org_id} → 정렬된 table_key 리스트."""
    weights = {**E28, **HYDE_WEIGHTS}
    scores = {}
    for name, keys in paths.items():
        w = weights.get(name, 0)
        if not w:
            continue
        for rank, key in enumerate((keys or []), 1):
            if key:
                scores[key] = scores.get(key, 0.0) + w / (rrf_k + rank)
    if orgs:
        for key in scores:
            if key.split(":", 1)[0] in orgs:
                scores[key] *= boost
    return [k for k, _ in sorted(scores.items(), key=lambda z: -z[1])]
