from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "diffurank_service" / "service.py"
SPEC = importlib.util.spec_from_file_location("diffurank_service_contract", MODULE_PATH)
assert SPEC and SPEC.loader
SERVICE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVICE
SPEC.loader.exec_module(SERVICE)


def _settings(tmp_path: Path):
    return SERVICE.RuntimeSettings(
        release_id="release-test",
        base_model_path=tmp_path / "base",
        adapter_path=tmp_path / "adapter",
        source_path=tmp_path / "source",
        device="cuda",
        rope_scaling_factor=4.0,
        max_candidates=3,
        max_input_tokens=1024,
        max_text_chars=100,
    )


def test_candidate_scope_hash_is_order_and_text_bound():
    first = (SERVICE.Candidate("a", "one"), SERVICE.Candidate("b", "two"))
    reordered = (SERVICE.Candidate("b", "two"), SERVICE.Candidate("a", "one"))
    altered = (SERVICE.Candidate("a", "ONE"), SERVICE.Candidate("b", "two"))
    assert SERVICE.canonical_candidate_scope_sha256(first) != SERVICE.canonical_candidate_scope_sha256(reordered)
    assert SERVICE.canonical_candidate_scope_sha256(first) != SERVICE.canonical_candidate_scope_sha256(altered)


def test_request_requires_current_release_and_exact_scope(tmp_path: Path):
    settings = _settings(tmp_path)
    candidates = (SERVICE.Candidate("table-a", "출생아 수"), SERVICE.Candidate("table-b", "취업자 수"))
    body = {
        "release_id": "release-test",
        "candidate_scope_sha256": SERVICE.canonical_candidate_scope_sha256(candidates),
        "query": "출생아",
        "candidates": [candidate.__dict__ for candidate in candidates],
    }
    parsed = SERVICE.parse_rerank_request(body, settings)
    assert parsed.candidates == candidates

    body["release_id"] = "other-release"
    with pytest.raises(SERVICE.ContractError, match="RELEASE_ID_MISMATCH"):
        SERVICE.parse_rerank_request(body, settings)


def test_request_rejects_scope_rewrite_and_duplicate_ids(tmp_path: Path):
    settings = _settings(tmp_path)
    candidates = (SERVICE.Candidate("same", "one"), SERVICE.Candidate("same", "two"))
    body = {
        "release_id": "release-test",
        "candidate_scope_sha256": "0" * 64,
        "query": "q",
        "candidates": [candidate.__dict__ for candidate in candidates],
    }
    with pytest.raises(SERVICE.ContractError, match="CANDIDATE_ID_DUPLICATE"):
        SERVICE.parse_rerank_request(body, settings)


def test_service_code_has_no_datastore_clients():
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
    for forbidden in (
        "import psycopg",
        "import opensearch",
        "import qdrant",
        "import redis",
        "requests.get",
        "urllib.request",
    ):
        assert forbidden not in source
