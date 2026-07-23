import json

from src.run_hcx_experiment_matrix import resolve


def test_matrix_config_contains_versioned_model_settings(tmp_path):
    config = {
        "common": {"temperature": 0.1},
        "models": [{"name": "HCX-003", "validation": {"prompt_version": "v1"}}],
        "input_validation": "data/evaluation/validation300.csv",
        "output_root": "output/matrix",
        "summary_file": "output/matrix/summary.csv",
        "metrics_log": "data/matrix.jsonl",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert resolve(config["input_validation"]).name == "validation300.csv"
    assert config["models"][0]["validation"]["prompt_version"] == "v1"
