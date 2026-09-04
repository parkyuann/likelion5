# 뉴스 수치 검증 파이프라인

기사 속 수치 주장을 공식 통계 근거와 대조하는 evidence-first 실행 파이프라인입니다. FastAPI 백엔드와 Vite UI가 기사 본문을 받아 주장과 값 후보를 추출하고, 검색·선택된 후보를 KOSIS 메타데이터의 항목·차원·기간과 binding한 뒤 공식 cell을 조회합니다. 마지막 비교와 근거 설명은 결정론적으로 수행됩니다.

## 흐름과 닫힌 실패

`기사 본문 → 수치 주장·후보 추출 → 검색/리랭킹 → KOSIS 메타데이터 binding → 공식 cell 조회 → 결정론 비교·근거 설명`

유일하게 호환되는 evidence-backed binding을 만들 수 없거나 외부 evidence를 얻지 못하면 임의의 답을 내지 않습니다. 필요한 경우 clarification을 요청하고, 그렇지 않으면 `UNVERIFIABLE`로 닫힙니다. Team MCP 자연어·자동 질의 경로는 현재 hard-disabled 503이며 이 공개 릴리스의 지원 기능이 아닙니다.

## 외부에 준비할 것

다음 의존성은 Git에 포함하지 않고 별도로 provision해야 합니다.

- PostgreSQL metadata/application DB와 Redis
- OpenSearch/BM25와 Qdrant 인덱스
- KOSIS·HCX 자격증명
- encoder/reranker model service와 모델 asset
- TLS 인증서와 키

실제 hostname, public IP, credential, server-only host path는 이 저장소에 넣지 않습니다.

## 설정 기본값

URL 입력, 이미지 입력, live stage, reranker, natural-query는 예시 환경에서 모두 `false`인 opt-in 경로입니다. 외부 의존성이 준비되지 않은 경로는 fake 결과나 임의 fallback 대신 명시적인 실패로 반환됩니다.

로컬에서 Docker 외의 경로를 검증할 때는 release root를 기준으로 다음 환경을 명시합니다.

```text
PIPELINE_RUNTIME_ROOT=<release>/deploy/pipeline_runtime
```

## Docker와 범위

Docker build는 저장소에 고정된 `deploy/pipeline_runtime` packaged closure만 API 이미지에 넣습니다. encoder image, 모델, 인덱스, receipt는 제공하지 않으며 외부에서 content-addressed 방식으로 provision해야 합니다.

이 릴리스는 source packaging과 공개 문서 정리를 제공할 뿐입니다. 성능 수치, live E2E 성공, production-ready 상태를 주장하지 않습니다.
