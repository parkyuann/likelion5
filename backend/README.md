# Backend — application overlay 상태

현재 운영 API는 `/api/auth/*`와 `/api/v1/*`를 제공한다.

- 인증: PostgreSQL `application` DB + `redis-session` opaque HttpOnly cookie
- 비밀번호: Argon2id
- CSRF: unsafe method의 exact Origin/Referer + Sec-Fetch-Site 검사
- session: 고정 7일 TTL, 사용자당 최신 5개
- SQLite 사용자/세션, Bearer token, URL query token: 운영 경로에서 사용하지 않음
- OAuth: `PENDING_OAUTH_CALLBACK_CONTRACT`
- 검색: `SEARCH_ADAPTER_PENDING`; 로컬 SQLite/v5/Qdrant fallback 없이 503 fail-closed
- application schema: `PENDING_APPLICATION_SCHEMA_RECONCILIATION`

실행 가능한 migration DDL은 아직 없다. `backend/migrations/`에는 read-only schema preflight와 후속 절차만 있다. EC2에서 migration, Compose 기동, 적재, 색인, release 활성화를 이 commit의 절차로 실행하면 안 된다.
