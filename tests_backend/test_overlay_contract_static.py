from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
WRITE_SET = {
    "app.py",
    "auth_dependencies.py",
    "auth_service.py",
    "database.py",
    "conversation_service.py",
    "favorites_service.py",
    "table_catalog_service.py",
    "runtime_gate.py",
    "session_store.py",
}


def test_backend_write_set_has_no_local_auth_or_catalog_path():
    source = "\n".join((BACKEND / name).read_text(encoding="utf-8") for name in WRITE_SET)
    lowered = source.lower()
    assert "import sqlite" not in lowered
    assert "sqlite3" not in lowered
    assert "httpbearer" not in lowered
    assert "access_token" not in lowered
    assert "authorization" not in lowered


def test_database_is_external_postgresql_without_bootstrap():
    source = (BACKEND / "database.py").read_text(encoding="utf-8").lower()
    assert "application_database_url" in source
    assert "postgresql" in source
    assert "CREATE TABLE " not in source.upper()
    assert "ALTER TABLE " not in source.upper()


def test_routes_are_api_namespaced_and_capabilities_are_fail_closed():
    tree = ast.parse((BACKEND / "app.py").read_text(encoding="utf-8"))
    routes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                    if isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "app":
                        if decorator.args and isinstance(decorator.args[0], ast.Constant):
                            routes.append((decorator.func.attr, decorator.args[0].value, node.name))
    assert ("post", "/api/auth/signup", "signup") in routes
    assert ("post", "/api/auth/login", "login") in routes
    assert ("post", "/api/auth/logout-all", "logout_all") in routes
    assert all(path == "/health" or path.startswith("/api/") for _, path, _ in routes)
    assert not any("/v1/auth" in path for _, path, _ in routes)
    source = (BACKEND / "app.py").read_text(encoding="utf-8")
    assert source.count("Depends(require_pipeline_runtime)") == 1
    assert source.count("Depends(require_application_product_state)") == 7
    assert "Depends(optional_user)" not in source[source.index('@app.get("/api/v1/tables")'):source.index('@app.get("/api/v1/favorites"')]


def test_redis_contract_is_fixed_and_atomic():
    source = (BACKEND / "session_store.py").read_text(encoding="utf-8")
    assert "SESSION_TTL_SECONDS = 604800" in source
    assert "MAX_ACTIVE_SESSIONS = 5" in source
    assert "auth:session:" in source
    assert "auth:user-sessions:" in source
    assert "redis.call('ZCARD'" in source
    assert "redis.call('ZRANGE'" in source
    assert "'EX', ARGV[2]" in source


def test_oauth_router_files_are_absent():
    assert not any((BACKEND / name).exists() for name in ("kakao_oauth.py", "naver_oauth.py", "google_oauth.py"))
