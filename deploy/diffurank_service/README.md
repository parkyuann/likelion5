# DiffuRank pointwise shadow service

This directory deploys the final DiffuRank LoRA adapter on the **instance 1
full-L4 node only**. It is intentionally separate from the instance 2
data/BGE application overlay.

## Fixed inputs

| Input | Fixed value |
|---|---|
| DiffusionRank source | `https://github.com/liuqi6777/DiffusionRank.git@8f38364f22db68a506e80a217add08fab739e8cf` |
| LLaDA base | `GSAI-ML/LLaDA-1.5@84346fd91ba60252d260022201ad6fc5a3468fb2` |
| tokenizer provenance | `liuqi6777/DiffuRank_Pointwise@d8298bdc049c5531ece2eeb936b3c6c2577d36c3` |
| final LoRA SHA-256 | `1108f9b5d0a287541b3440affd9080f8f76ef6c5fe536522a9645876af541d49` |
| final train run | `20260821T022022Z` |

The adapter is LoRA-only. `service.py` loads the pinned base first and then
attaches the local adapter with `PeftModel.from_pretrained`. Loading only the
base model is a contract failure.

## Authority boundary

The service accepts only a release-pinned, fixed candidate scope and returns
the same candidate IDs with scores/ranks. It has no database clients and never
creates vectors, changes an OpenSearch index, writes Qdrant, calls KOSIS, binds
axes, selects a final table/cell, or produces a verdict.

The caller must keep `DIFFURANK_ENABLED=false` until the shadow evaluation and
the KOSIS retrieval owner approve an activation. The original final
architecture's Late Binding, Strict Validator, Param API, and deterministic
comparison remain downstream of this service.

## One-time base download and preflight

Run the following on instance 1 after the image is built. The model cache is
kept on the persistent EBS path, not instance-store.

```bash
sudo docker run --rm --gpus all --user 0 \
  -e DIFFURANK_BASE_MODEL_PATH=/models/base \
  -v /srv/news_verification/diffurank/models/base:/models/base \
  news-verification-diffurank:<receipt-bound-tag> \
  python download_base.py

sudo docker run --rm --gpus all \
  -e DIFFURANK_RELEASE_ID=kosis_canonical_20260821_full_r3_13ko_views \
  -e DIFFURANK_BASE_MODEL_PATH=/models/base \
  -e DIFFURANK_ADAPTER_PATH=/models/adapter \
  -e DIFFURANK_ROPE_SCALING_FACTOR=4.0 \
  -v /srv/news_verification/diffurank/models/base:/models/base:ro \
  -v /srv/news_verification/diffurank/models/final_adapter:/models/adapter:ro \
  -v /srv/news_verification/diffurank/receipts:/receipts \
  news-verification-diffurank:<receipt-bound-tag> \
  python preflight.py --output /receipts/diffurank_preflight.json
```

`4.0` comes from the pinned pointwise LoRA training YAML. It is a recorded
shadow setting, not an approval to make DiffuRank active. The KOSIS-specific
candidate evaluation decides whether and how it becomes active.

## Inter-node connection

When the preflight is accepted, bind port `8203` to instance 1's private VPC
address only and permit inbound traffic only from instance 2's private address
or security group. Instance 2 later receives an optional client endpoint such
as `http://172.31.7.236:8203`; it must send `release_id`, the canonical
candidate-scope SHA-256, candidate IDs/text, and a server-only internal token.

Do not expose this port publicly and do not add this service to instance 2's
`deploy/compose.yaml`.
