# Application migration source 경계

EC2에는 정본 `001_application_auth`가 적용됐고 전달된 SHA-256은
`99ACC4235A6E6E21B0802E6163EAA09B01C0BFE3534907D1AAD395404B523387`이다.
001 SQL 원문은 이 저장소에 없으므로 재작성·수정·재실행하지 않는다. API runtime은 migration이나
schema bootstrap을 실행하지 않고, `VERIFIED` 및 exact revision만 fail-closed 확인한다.

적용 전 절차:

1. 필요 시 migration owner의 별도 승인 절차에서 `preflight_application_schema.sql`을 읽기 전용으로 실행한다.
2. 결과와 제공된 001 정본 receipt를 대조한다.
3. conversations/messages/favorites는 기존 SQLite ERD를 재사용하지 않고 별도
   `002_application_product_state` 설계·승인 후 additive migration으로 발급한다.
4. `application_runtime`에는 승인된 DML 권한만 부여한다.

이 디렉터리의 현재 파일은 migration 실행물이 아니며 EC2에서 실행하지 않았다.
