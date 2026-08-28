FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY configs ./configs

# The materialized closure is already filtered to the approved runtime files;
# neither legacy root entrypoints nor archive data enter the API image.
COPY deploy/pipeline_runtime/src ./pipeline_runtime/src
RUN chmod -R a=rX /app

ENV PIPELINE_RUNTIME_ROOT=/app/pipeline_runtime
# The API and encoder share one fixed non-root uid so they can read the same
# read-only, server-owned encoder authentication token.
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
