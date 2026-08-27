FROM news-verification-bge-preflight:py311-torch213-cu130@sha256:b98d5ae3c07824c21e2f0242e9cf488c47563a6c0320e9876a4af245f7538adb

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    BGE_QUERY_ENCODER_RUNTIME_LOCK_PATH=/app/encoder-runtime.lock.json \
    BGE_QUERY_ENCODER_MODEL_CLOSURE_PATH=/app/bge-model-closure-7074d66a.json \
    BGE_QUERY_ENCODER_MODEL_REPOSITORY=/models/repository \
    BGE_QUERY_ENCODER_MODEL_REVISION=7074d66aa46562342193ca4feb3d89bf9dad71b4 \
    BGE_QUERY_ENCODER_PORT=8101

WORKDIR /app
COPY backend/query_encoder_service.py /app/query_encoder_service.py
COPY deploy/encoder-runtime.lock.json /app/encoder-runtime.lock.json
COPY deploy/bge-model-closure-7074d66a.json /app/bge-model-closure-7074d66a.json
RUN chmod 0555 /app/query_encoder_service.py \
    && chmod 0444 /app/encoder-runtime.lock.json /app/bge-model-closure-7074d66a.json

USER 65532:65532
ENTRYPOINT ["python3", "/app/query_encoder_service.py"]
