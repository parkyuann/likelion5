from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose.yaml"
NGINX = ROOT / "deploy" / "nginx.conf"
RUNTIME_ENV = ROOT / "deploy" / "runtime.env.example"
INVENTORY = ROOT / "DEPLOYMENT_INVENTORY.md"


def _section(source: str, heading: str, next_heading: str | None = None) -> str:
    start = source.index(heading)
    end = source.find(next_heading, start + len(heading)) if next_heading else len(source)
    return source[start:end if end >= 0 else len(source)]


def test_compose_contains_only_application_overlay_services():
    source = COMPOSE.read_text(encoding="utf-8")
    services = _section(source, "services:\n", "\nnetworks:")
    service_names = {
        line.strip()[:-1]
        for line in services.splitlines()
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":")
    }
    assert service_names == {"api", "bge-query-encoder", "nginx"}
    for forbidden in ("postgres", "opensearch", "qdrant", "redis-session", "redis-cache", "reranker", "diffurank"):
        assert f"\n  {forbidden}:" not in services.lower()


def test_encoder_is_internal_gpu_service_with_read_only_attestations():
    source = COMPOSE.read_text(encoding="utf-8")
    encoder = _section(source, "  bge-query-encoder:\n", "\n  nginx:")
    assert 'image: "sha256:666dea4633e530cda4959c2b5682920ff408e8754b58fe728d787256bae9beb3"' in encoder
    assert "BGE_QUERY_ENCODER_IMAGE" not in encoder
    assert 'expose: ["8101"]' in encoder
    assert "ports:" not in encoder
    assert "encoder_internal" in encoder
    assert "count: 1" in encoder and "capabilities: [gpu]" in encoder
    assert "target: /models/repository" in encoder
    assert "target: /etc/bge-query-encoder/receipt/query_encoder_preflight_20260827.json" in encoder
    assert "target: /etc/bge-query-encoder/closure/bge-model-closure-7074d66a.json" in encoder
    assert encoder.count("read_only: true") >= 3
    assert "secrets:" in encoder and "bge_query_encoder_token" in encoder


def test_encoder_base_and_supersession_are_explicitly_pinned():
    dockerfile = (ROOT / "deploy" / "encoder.Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.splitlines()[0] == (
        "FROM news-verification-bge-preflight:py311-torch213-cu130@sha256:"
        "b98d5ae3c07824c21e2f0242e9cf488c47563a6c0320e9876a4af245f7538adb"
    )
    supersession = (ROOT / "deploy" / "DESIGN_SUPERSESSION_BGE_QUERY_ENCODER_V2_20260827.md").read_text(encoding="utf-8")
    assert 'using="dense"` 요구는' in supersession
    assert "QDRANT_VECTOR_NAME`은 빈 문자열만 허용" in supersession
    assert "grouped query에 `using`을 보내지 않는다" in supersession


def test_api_has_both_networks_and_datastore_network_is_external():
    source = COMPOSE.read_text(encoding="utf-8")
    api = _section(source, "  api:\n", "\n  bge-query-encoder:")
    assert "kosis_shadow_internal" in api and "encoder_internal" in api
    assert "external: true" in source
    assert "name: kosis_shadow_internal" in source
    assert "\n  api:" in source and "\n  nginx:" in source and "\n  bge-query-encoder:" in source


def test_nginx_has_dev_tls_redirect_and_preserves_api_prefix():
    source = NGINX.read_text(encoding="utf-8")
    assert "listen 8080;" in source
    assert "return 308 https://$host:8443$request_uri;" in source
    assert "listen 8443 ssl;" in source
    assert "ssl_certificate /etc/nginx/tls/dev.crt;" in source
    assert "ssl_certificate_key /etc/nginx/tls/dev.key;" in source
    api = _section(source, "location /api/", "location = /health")
    assert "proxy_pass http://api:8000;" in api
    assert "proxy_set_header X-Forwarded-Proto https;" in api


def test_runtime_pins_hybrid_and_pending_feature_gates():
    source = RUNTIME_ENV.read_text(encoding="utf-8")
    expected = {
        "KOSIS_RELEASE_ID=kosis_canonical_20260821_full_r3_13ko_views",
        "KOSIS_BM25_ANALYZER=standard-v1",
        "QDRANT_VECTOR_SIZE=1024",
        "QDRANT_VECTOR_NAME=",
        "BGE_QUERY_ENCODER_ENABLED=true",
        "BGE_QUERY_ENCODER_MODEL_REVISION=7074d66aa46562342193ca4feb3d89bf9dad71b4",
        "BGE_QUERY_ENCODER_MODEL_RECEIPT_SHA256=e092f65d5520f374e30c647f6f02d8203b4ddc6ddfd5d064acfd87f6bb28dff7",
        "BGE_RERANKER_ENABLED=false",
        "KOSIS_REDIS_CACHE_ENABLED=false",
        "PIPELINE_RUNTIME_ENABLED=true",
        "PIPELINE_LIVE_STAGE_ENABLED=false",
        "PIPELINE_NATURAL_QUERY_ENABLED=false",
        "PIPELINE_IMAGE_ENABLED=false",
        "PIPELINE_URL_ENABLED=false",
    }
    assert expected <= set(source.splitlines())
    assert "BGE_QUERY_ENCODER_TOKEN_SOURCE=/srv/news_verification/application-overlay/secrets/bge_query_encoder_token" in source
    assert "NGINX_TLS_CERT_SOURCE=/srv/news_verification/application-overlay/tls/dev.crt" in source
    assert "NGINX_TLS_KEY_SOURCE=/srv/news_verification/application-overlay/tls/dev.key" in source


def test_inventory_keeps_auth_write_boundary_and_pending_items_explicit():
    source = INVENTORY.read_text(encoding="utf-8")
    assert "users`와" in source and "auth_accounts`의 signup/login 관련 DML" in source
    assert "application_schema_migrations` 변경" in source
    assert "Qdrant\nupsert/delete/recreate/write" in source
    assert "BGE reranker runtime enablement" in source
    assert "`002_application_product_state`" in source
    assert "commit SHA: RECORDED_IN_HANDOFF_RESPONSE" in source
