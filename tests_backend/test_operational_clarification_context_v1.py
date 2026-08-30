from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def test_operational_clarification_hydrates_date_and_full_provenance_for_monthly_and_annual_resume():
    repo_root = Path(__file__).parents[1].resolve()
    runtime_root = repo_root / "deploy" / "pipeline_runtime"
    script = f"""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
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
body = "지난 4월 출생아는 100명이다. 지난해 합계출산율은 0.8명이다."
body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
answer = {{
    "question_id": "clarify-article_date",
    "role": "article_date",
    "value": "2026-08-26",
}}
with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    article_path = root / "articles.jsonl"
    context_path = root / "clarification_context.json"
    article_path.write_text(json.dumps({{
        "article_idx": "article-1",
        "title": "population",
        "article_text": body,
        "date": "",
    }}, ensure_ascii=False) + "\\n", encoding="utf-8")
    context_path.write_text(json.dumps({{
        "contract_version": "clarification-context-v1",
        "article_body_sha256": body_sha,
        "clarification_answers": [answer],
    }}, ensure_ascii=False), encoding="utf-8")

    loaded = module._load_articles_for_clarification(article_path, context_path)
    assert len(loaded) == 1
    article = loaded[0]
    assert article["date"] == "2026-08-26"
    assert article["article_date"] == "2026-08-26"
    provenance = article["article_date_provenance"]
    assert provenance == {{
        "source": "USER_CLARIFICATION",
        "question_id": "clarify-article_date",
        "role": "article_date",
        "date_source": "user_feedback",
        "source_path": "clarification_context",
        "date_field": "date",
        "article_text_sha256": body_sha,
        "answer_sha256": module._clarification_answer_sha(answer),
    }}

    answer_only_article = {{"article_text": body, "clarification_answers": [answer]}}
    monthly = module._merge_user_clarifications(
        {{"period_raw": "지난 4월", "article_text": body}}, answer_only_article
    )
    annual = module._merge_user_clarifications(
        {{"period_raw": "지난해", "article_text": body}}, answer_only_article
    )
    for resumed in (monthly, annual):
        assert resumed["date"] == "2026-08-26"
        assert resumed["article_date"] == "2026-08-26"
        assert resumed["article_date_provenance"] == provenance

    immutable = json.loads(article_path.read_text(encoding="utf-8"))
    assert immutable["date"] == ""
    assert immutable["article_text"] == body
print("CLARIFICATION_HYDRATION_OK")
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
    assert "CLARIFICATION_HYDRATION_OK" in result.stdout
