"""Download the exact public LLaDA base snapshot into a persistent host mount."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from huggingface_hub import snapshot_download

from service import BASE_MODEL_ID, BASE_MODEL_REVISION


def main() -> None:
    target = Path(os.environ.get("DIFFURANK_BASE_MODEL_PATH", "/models/base"))
    target.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_download(
        repo_id=BASE_MODEL_ID,
        revision=BASE_MODEL_REVISION,
        local_dir=str(target),
        local_dir_use_symlinks=False,
    )
    required = ("config.json", "model.safetensors.index.json", "modeling_llada.py", "tokenizer.json")
    missing = [name for name in required if not (target / name).is_file()]
    if missing:
        raise RuntimeError("BASE_SNAPSHOT_INCOMPLETE:" + ",".join(missing))
    receipt = {
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "snapshot_path": str(snapshot),
        "required_files": list(required),
    }
    receipt_path = target / ".download-receipt.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target, delete=False) as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(receipt_path)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
