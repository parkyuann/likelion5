from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_verification_entries_fail_closed_when_release_sha_is_unknown_or_mismatched() -> None:
    source = (ROOT / "frontend/src/api.js").read_text(encoding="utf-8")

    assert 'VITE_APP_RELEASE_SHA || "unknown"' in source
    assert "const RELEASE_SHA_RE = /^[0-9a-f]{40}$/;" in source
    assert "DEPLOYMENT_VERSION_UNAVAILABLE" in source
    assert "DEPLOYMENT_VERSION_MISMATCH" in source
    verify_source = source[source.index("export async function verifyArticleDevelop"):source.index("export async function analyzeImage")]
    image_source = source[source.index("export async function analyzeImage"):source.index("export async function checkHealth")]
    assert "await checkReleaseVersion();" in verify_source
    assert "await checkReleaseVersion();" in image_source


def test_status_labels_and_compose_build_contract_preserve_one_valid_sha() -> None:
    chat = (ROOT / "frontend/src/ChatApp.jsx").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    api_dockerfile = (ROOT / "deploy/api.Dockerfile").read_text(encoding="utf-8")
    nginx_dockerfile = (ROOT / "deploy/nginx.Dockerfile").read_text(encoding="utf-8")

    for label in ("검증 완료", "일부 근거 확인 · 추가 확인 필요", "공식 통계 근거를 확인하지 못함", "구조화 완료 · 공식 대조 미실행"):
        assert label in chat
    assert "APP_RELEASE_SHA:?set the exact 40-character Git SHA" in compose
    assert ":-unknown" not in compose
    assert "^[0-9a-f]{40}$" in api_dockerfile
    assert "^[0-9a-f]{40}$" in nginx_dockerfile
