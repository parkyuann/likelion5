# 운영 목업 차단·구형 ERD 실행 금지 설계 — 20260827

## 목적

운영 frontend에서 URL query만으로 고정 목업 결과나 목업 지연을 활성화할 수 없게 하고,
현재 PostgreSQL/Redis application overlay와 충돌하는 구형 SQLite/Bearer ERD가 구현 정본으로
오인되지 않게 한다.

## 확인된 외부 계약

- 적용 기준 migration: `001_application_auth.sql`
- 전달된 SHA-256: `99ACC4235A6E6E21B0802E6163EAA09B01C0BFE3534907D1AAD395404B523387`
- live application DB 테이블: `users`, `auth_accounts`, `application_schema_migrations`
- `auth_accounts.provider` 허용값: `local`, `kakao`, `naver`, `google`
- `provider_user_id`는 필수이며 local 계정은 `password_hash`, `provider_email`이 필요하다.
- local `provider_user_id` 생성 규칙은 아직 미확정이다.

위 정보는 전달된 read-only 확인 결과로 기록한다. 이 변경에서는 migration 원문 또는 live DB를
재조회하지 않으며 인증 SQL을 수정하지 않는다.

## 승인 요청 변경

1. `frontend/src/ChatApp.jsx`
   - `mockEnabled()`와 `mockDelayOverrideMs()` 모두 `import.meta.env.DEV`가 참일 때만 URL query를 읽는다.
   - production build에서는 `?mock=1`, `?mock=true`, `?mockDelay=N`이 항상 비활성이다.
   - 별도 production enable 환경변수나 우회 경로는 추가하지 않는다.
2. `backend/ERD.md`
   - 파일 최상단에 `LEGACY_DO_NOT_IMPLEMENT` 경고를 추가한다.
   - SQLite/Bearer 기반 과거 제안이며 현재 application DB/migration 정본이 아님을 명시한다.
3. `backend/schema.erd`
   - Mermaid comment 형식의 `LEGACY_DO_NOT_IMPLEMENT` 경고를 최상단에 추가한다.
   - 기존 다이어그램 본문은 역사적 참고를 위해 보존한다.

## 명시적 비범위

- `backend/auth_service.py` 수정
- `001_application_auth.sql` 작성·수정·추정
- `002_application_product_state.sql` 설계 또는 작성
- Compose, Nginx, HTTPS, 검색 adapter, pipeline 변경
- EC2 접속, container 기동, migration 실행, 데이터 변경
- `TASKS.md` 갱신: 본 변경은 주 파이프라인 상태를 바꾸지 않는다.

## 검증

- 정적 검색으로 production mock 우회가 남지 않았는지 확인한다.
- `npm run lint`, `npm run build`를 실행한다.
- build 산출물에 query 문자열이 남을 수는 있으나, production 분기에서 목업 활성화 함수가
  항상 false/null임을 source와 Vite 상수 치환 결과로 확인한다.
- 변경 파일이 승인 범위를 벗어나지 않았는지 Git diff로 확인한다.

## 후속 PENDING

- `PENDING_LOCAL_PROVIDER_USER_ID_RULE`
- `PENDING_AUTH_SCHEMA_ALIGNMENT`
- `PENDING_APPLICATION_PRODUCT_STATE_002`
- `PENDING_HTTPS_DOMAIN_CERTIFICATE`
- `SEARCH_ADAPTER_PENDING`
