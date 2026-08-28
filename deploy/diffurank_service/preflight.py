"""GPU-only, write-once DiffuRank model preflight receipt generator."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from service import (
    Candidate,
    RerankRequest,
    RuntimeSettings,
    canonical_candidate_scope_sha256,
    load_runtime,
    response_fingerprint,
    rerank,
    runtime_identity,
)


PROBE_QUERY = "월별 출생아 수 증가 통계를 찾는다"
PROBE_CANDIDATES = (
    Candidate("probe_population_births", "월별 출생아 수와 전년동월 대비 증감 정보를 제공하는 통계표"),
    Candidate("probe_unrelated", "산업별 취업자 수를 제공하는 통계표"),
)


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    settings = RuntimeSettings.from_env(require_internal_token=False)
    runtime = load_runtime(settings)
    request = RerankRequest(
        release_id=settings.release_id,
        candidate_scope_sha256=canonical_candidate_scope_sha256(PROBE_CANDIDATES),
        query=PROBE_QUERY,
        candidates=PROBE_CANDIDATES,
    )
    first = rerank(runtime, request)
    second = rerank(runtime, request)
    first_hash = response_fingerprint(first)
    receipt = {
        **runtime_identity(runtime),
        "preflight": "diffurank-pointwise-gpu-preflight-v1",
        "release_id": settings.release_id,
        "candidate_count": len(PROBE_CANDIDATES),
        "results_sha256": first_hash,
        "repeat_results_sha256": response_fingerprint(second),
        "repeat_exact": first == second,
        "input_texts_recorded": False,
        "status": "READY" if first == second else "FAILED_NONDETERMINISTIC",
    }
    _write_atomic(Path(args.output), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
