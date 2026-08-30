FROM python:3.13-slim AS runtime
ARG APP_RELEASE_SHA
RUN test "${#APP_RELEASE_SHA}" -eq 40 && printf '%s' "$APP_RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$'
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV APP_RELEASE_SHA=${APP_RELEASE_SHA}
LABEL org.opencontainers.image.revision=${APP_RELEASE_SHA}
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY configs ./configs

# The materialized closure is already filtered to the approved runtime files;
# neither legacy root entrypoints nor archive data enter the API image.
COPY deploy/pipeline_runtime/src ./pipeline_runtime/src
COPY deploy/pipeline_runtime/manifest.json ./pipeline_runtime/manifest.json
RUN chmod -R a=rX /app

ENV PIPELINE_RUNTIME_ROOT=/app/pipeline_runtime
# The API and encoder share one fixed non-root uid so they can read the same
# read-only, server-owned encoder authentication token.
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
