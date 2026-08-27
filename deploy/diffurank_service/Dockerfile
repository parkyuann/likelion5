# The original DiffusionRank requirement was torch 2.6.0 with CUDA 12.4.
# Never replace this digest with a mutable tag without a new receipt.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:77f17f843507062875ce8be2a6f76aa6aa3df7f9ef1e31d9d7432f4b0f563dee

ARG DIFFUSIONRANK_REPOSITORY=https://github.com/liuqi6777/DiffusionRank.git
ARG DIFFUSIONRANK_COMMIT=8f38364f22db68a506e80a217add08fab739e8cf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/diffurank-service:/opt/diffusionrank/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/requirements.lock

# The public upstream implementation is a model/evaluation library, not a
# network service.  This image wraps the exact source commit with the local
# candidate-only service defined in this directory.
RUN git clone "${DIFFUSIONRANK_REPOSITORY}" /opt/diffusionrank \
    && git -C /opt/diffusionrank checkout --detach "${DIFFUSIONRANK_COMMIT}" \
    && test "$(git -C /opt/diffusionrank rev-parse HEAD)" = "${DIFFUSIONRANK_COMMIT}" \
    && rm -rf /opt/diffusionrank/.git

WORKDIR /opt/diffurank-service
COPY . /opt/diffurank-service

RUN useradd --system --uid 10001 --create-home --home-dir /nonexistent diffurank \
    && chown -R diffurank:diffurank /opt/diffurank-service

USER diffurank
EXPOSE 8203
CMD ["python", "-m", "uvicorn", "service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8203", "--workers", "1"]
