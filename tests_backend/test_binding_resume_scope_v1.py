from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from backend import verification_checkpoint_store as store

RUNTIME_ROOT = Path(__file__).parents[1] / "deploy" / "pipeline_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from src.news_verification.runtime.run_pipeline_operational_v2 import (
    OperationalPipelineError,
    _binding_or_retrieve_candidates,
    _validate_binding_continuation_runtime,
)


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _continuation(target_id="article:target-1"):
    profile = {"table_key": "101:T1", "release_id": "release-1", "items": [], "dimensions": [], "periods": []}
    profile["profile_sha256"] = _sha(profile)
    membership = ["101:T1"]
    receipt = [{"table_key": "101:T1", "profile_sha256": profile["profile_sha256"], "release_id": "release-1"}]
    return {
        "contract_version": "binding-continuation-v1",
        "target_ids": [target_id], "target_scope_sha256": _sha([target_id]),
        "candidate_membership": membership, "candidate_membership_sha256": _sha(membership),
        "raw_profiles": {"101:T1": profile},
        "projection_profiles": {}, "projection_bundle_sha256": _sha({}),
        "profile_bundle_sha256": _sha(receipt), "release_id": "release-1",
    }


def test_binding_resume_is_target_scoped_and_physical_retrieval_is_zero():
    continuation = _continuation()
    scope, membership, _ = _validate_binding_continuation_runtime(continuation, expected_release_id="release-1")
    calls = {"count": 0}

    def retrieve():
        calls["count"] += 1
        return (), {"physical_calls": 1}

    candidates, audit = _binding_or_retrieve_candidates(
        target_id="article:target-1", target_scope=scope, membership=membership, retrieve=retrieve,
    )
    assert [item.table_key for item in candidates] == ["101:T1"]
    assert audit["physical_calls"] == 0
    assert calls["count"] == 0

    _binding_or_retrieve_candidates(
        target_id="article:target-2", target_scope=scope, membership=membership, retrieve=retrieve,
    )
    assert calls["count"] == 1


def test_checkpoint_consume_rejects_profile_sha_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path / "checkpoints"))
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    (work / "articles.jsonl").write_text(json.dumps({"article_idx": "article", "article_text": "본문"}) + "\n", encoding="utf-8")
    continuation = _continuation()
    cp = store.create(
        workdir=work, article_body_sha256="a" * 64, title="제목", article_id="article",
        clarification_history=[], runtime_fingerprint="r" * 64, config_sha256="c" * 64,
        resume_from_stage="binding",
        clarification_plan={"contract_version": "clarification-plan-v2", "question": {"id": "cq-1", "role": "region", "input_mode": "FREE_TEXT"}},
        binding_continuation=continuation,
    )
    path = cp.root / "binding_continuation.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["raw_profiles"]["101:T1"]["items"] = [{"itm_id": "forged"}]
    payload = (json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(payload)
    metadata_path = cp.root / "checkpoint.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["artifact_records"]["binding_continuation.json"] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    metadata["binding_continuation_sha256"] = hashlib.sha256(payload).hexdigest()
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(store.CheckpointError, match="RESUME_ARTIFACT_INVALIDATED"):
        store.consume(
            cp.token, article_body_sha256="a" * 64, title="제목",
            clarification_history=[{"question_id": "cq-1", "role": "region", "value": "전국"}],
            runtime_fingerprint="r" * 64, config_sha256="c" * 64,
            expected_question_id="cq-1", expected_role="region",
        )


def test_runtime_rejects_release_or_profile_mismatch():
    continuation = _continuation()
    with pytest.raises(OperationalPipelineError, match="RESUME_ARTIFACT_INVALIDATED"):
        _validate_binding_continuation_runtime(continuation, expected_release_id="release-2")
    continuation["raw_profiles"]["101:T1"]["profile_sha256"] = "0" * 64
    with pytest.raises(OperationalPipelineError, match="RESUME_ARTIFACT_INVALIDATED"):
        _validate_binding_continuation_runtime(continuation, expected_release_id="release-1")
