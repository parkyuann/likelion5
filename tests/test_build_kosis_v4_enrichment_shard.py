import json

from src.build_kosis_v4_enrichment_shard import build_shard, shard_for


def test_shard_builder_selects_only_registry_only_rows_deterministically(tmp_path):
    registry = tmp_path / "registry.jsonl"
    rows = [
        {"table_key": "1:T1", "org_id": "1", "tbl_id": "T1", "tbl_name": "One", "metadata_status": "registry_only", "category_paths": [["A"]]},
        {"table_key": "1:T2", "org_id": "1", "tbl_id": "T2", "tbl_name": "Two", "metadata_status": "enriched"},
        {"table_key": "2:T3", "org_id": "2", "tbl_id": "T3", "tbl_name": "Three", "metadata_status": "registry_only", "category_paths": [["B"]]},
    ]
    registry.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    output = tmp_path / "shard.jsonl"
    target = shard_for("1:T1", 2)

    manifest = build_shard(registry, output, shard_index=target, shard_count=2, metadata_status="registry_only")
    selected = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert manifest["eligible_tables"] == 2
    assert all(row["sample_source"] == f"background_registry_shard:{target + 1}/2" for row in selected)
    assert all(row["table_key"] != "1:T2" for row in selected)
