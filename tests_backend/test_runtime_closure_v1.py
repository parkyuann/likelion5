from __future__ import annotations

from pathlib import Path

from deploy.release_manifest import verify_runtime_closure


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_closure_verifier_reports_the_fixed_74_path_contract() -> None:
    receipt = verify_runtime_closure(ROOT)

    assert receipt["expected"] == 74
    assert receipt["matched"] + len(receipt["mismatches"]) == 74
    assert isinstance(receipt["manifest_sha256"], str) and len(receipt["manifest_sha256"]) == 64
