# BGE query encoder V2 vector 계약 대체 기록 — 20260827

## 결정

`DESIGN_BGE_QUERY_ENCODER_HYBRID_RETRIEVAL_V2_20260827.md`의 named-vector 전제 중
`using="dense"` 요구는 실제 EC2 collection 실측 전에 작성된 설계이므로 더 이상 구현 정본이 아니다.
해당 문서의 byte와 당시 SHA receipt는 역사적 기록으로 보존하며 수정하지 않는다.

현재 구현 정본은 승인된 `DESIGN_EC2_INTEGRATED_DEV_ENV_V1_20260827.md`의 unnamed-vector
계약이다. 실제 collection은 단일 unnamed 1024-dimension cosine vector이며 backend는 다음을
fail-closed로 적용한다.

- `QDRANT_VECTOR_NAME`은 빈 문자열만 허용한다.
- Qdrant grouped query에 `using`을 보내지 않는다.
- named vector, 특히 `dense`, 로 자동 전환하거나 재시도하지 않는다.
- collection schema가 unnamed/1024/cosine/green과 다르면 후보 검색을 중단한다.

이 대체 기록은 초기 V2 문서 전체를 폐기하지 않는다. named-vector 항목과 그에 직접 종속된
환경변수 설명만 대체하며 model revision, receipt, normalized 1024 query vector, read-only 검색,
RRF와 fail-closed 원칙은 유지한다.
