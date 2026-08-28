from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_canonical_operational_import_closes_without_annual_cycle():
    repo_root = Path(__file__).parents[1].resolve()
    runtime_root = repo_root / "deploy" / "pipeline_runtime"
    script = f"""
import importlib
import sys
import types

sys.path.insert(1, {str(repo_root)!r})

# The canonical module imports these optional clients at module load.  Keep
# this regression self-contained while importing the complete canonical module
# and the real annual helper; no production module is replaced.
requests = types.ModuleType("requests")
requests.RequestException = RuntimeError
requests.get = lambda *args, **kwargs: None
requests.post = lambda *args, **kwargs: None
requests.Session = lambda: None
sys.modules["requests"] = requests
pandas = types.ModuleType("pandas")
pandas.Series = object
pandas.DataFrame = object
sys.modules["pandas"] = pandas

importlib.import_module("src.news_verification.runtime.run_pipeline_operational_v2")
print("IMPORT_OK")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_root) + os.pathsep + str(repo_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=runtime_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "IMPORT_OK" in result.stdout
    assert "partially initialized" not in result.stderr.lower()


def test_generic_annual_result_can_create_evidence_first_ledger_projection():
    repo_root = Path(__file__).parents[1].resolve()
    runtime_root = repo_root / "deploy" / "pipeline_runtime"
    script = f"""
import importlib
import json
import sys
import types

sys.path.insert(1, {str(repo_root)!r})
requests = types.ModuleType("requests")
requests.RequestException = RuntimeError
requests.get = lambda *args, **kwargs: None
requests.post = lambda *args, **kwargs: None
requests.Session = lambda: None
sys.modules["requests"] = requests
pandas = types.ModuleType("pandas")
pandas.Series = object
pandas.DataFrame = object
sys.modules["pandas"] = pandas

module = importlib.import_module("src.news_verification.runtime.run_pipeline_operational_v2")
generic_annual = module.Top50Resolution(
    resolution=None, projections=(), candidate_membership=(), projected_count=0,
)
ledger = module._evidence_first_ledger_projection(
    generic_annual, target_id="annual-change", article_date_limitation="date-limitation",
)
assert ledger["target_id"] == "annual-change"
assert ledger["profile_receipts"] == []
assert ledger["membership_receipt_sha256"] is None
print(json.dumps(ledger, ensure_ascii=False, sort_keys=True))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_root) + os.pathsep + str(repo_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=runtime_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"profile_receipts": []' in result.stdout
    assert '"membership_receipt_sha256": null' in result.stdout
