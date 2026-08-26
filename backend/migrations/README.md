# Application migration source — PENDING

`application` DB의 기존 정본 migration과 실제 `users`/`auth_accounts` 컬럼이 공유되지 않아 DDL을 추정하지 않는다. API runtime은 migration이나 schema bootstrap을 실행하지 않는다.

적용 전 절차:

1. migration owner 권한의 별도 세션에서 `preflight_application_schema.sql`을 읽기 전용으로 실행한다.
2. 결과를 기존 정본 migration과 대조한다.
3. `PENDING_APPLICATION_SCHEMA_RECONCILIATION`을 해소한 뒤 additive migration 번호를 발급한다.
4. `application_runtime`에는 필요한 DML 권한만 부여한다.

이 디렉터리의 현재 파일은 migration 실행물이 아니며 EC2에서 실행하지 않았다.
