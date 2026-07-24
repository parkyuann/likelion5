"""전체 v4 registry에서 중단·재개 가능한 metadata enrichment shard seed를 만든다."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def shard_for(table_key: str, shard_count: int) -> int:
    """입력 순서와 실행 장비가 달라도 같은 표가 같은 shard에 들어가게 한다."""
    return int(hashlib.sha256(table_key.encode("utf-8")).hexdigest(), 16) % shard_count


def seed_from_registry(record: dict[str, Any], shard_index: int, shard_count: int) -> dict[str, Any]:
    paths = record.get("category_paths") if isinstance(record.get("category_paths"), list) else []
    category_path = paths[0] if paths and isinstance(paths[0], list) else []
    return {
        "table_key": str(record["table_key"]),
        "org_id": str(record["org_id"]),
        "tbl_id": str(record["tbl_id"]),
        "tbl_name": str(record.get("tbl_name") or ""),
        "stat_id": str(record.get("stat_id") or ""),
        "category_path": [str(value) for value in category_path],
        "sample_source": f"background_registry_shard:{shard_index + 1}/{shard_count}",
    }


def build_shard(
    registry_path: Path, output_path: Path, *, shard_index: int, shard_count: int, metadata_status: str,
) -> dict[str, Any]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    selected = 0
    eligible = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open(encoding="utf-8") as source, output_path.open("w", encoding="utf-8") as output:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"{registry_path}:{line_number} is not a JSON object")
            if str(record.get("metadata_status") or "") != metadata_status:
                continue
            table_key = str(record.get("table_key") or "")
            if not table_key or not record.get("org_id") or not record.get("tbl_id"):
                continue
            eligible += 1
            if shard_for(table_key, shard_count) != shard_index:
                continue
            output.write(json.dumps(seed_from_registry(record, shard_index, shard_count), ensure_ascii=False) + "\n")
            selected += 1
    return {
        "registry_path": str(registry_path),
        "metadata_status_filter": metadata_status,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "eligible_tables": eligible,
        "selected_tables": selected,
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic KOSIS v4 background enrichment seed shard")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shard-index", type=int, required=True, help="zero-based shard index")
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--metadata-status", default="registry_only")
    args = parser.parse_args()
    print(json.dumps(build_shard(
        args.registry, args.output, shard_index=args.shard_index, shard_count=args.shard_count,
        metadata_status=args.metadata_status,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
