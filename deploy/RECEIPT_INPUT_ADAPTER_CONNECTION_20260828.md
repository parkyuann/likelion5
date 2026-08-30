# URL·이미지 입력 어댑터 연결 receipt — 2026-08-28

## 범위와 release 경계

- 구현 commit: `bb4456052d7e297a125be1534e43365eea9fd1ef`
- 대상: EC2 application overlay의 API·Nginx 및 browser 입력 경로
- 유지: `kosis_canonical_20260821_full_r3_13ko_views` 데이터 release와 기존 PostgreSQL·OpenSearch·Qdrant·`redis-session` volume
- 비범위: 재크롤링, PostgreSQL write, OpenSearch 재색인, Qdrant write/재임베딩, `redis-cache` 활성화

## URL 기사 입력

- 공개 API: `POST /api/v1/analyze` (`input_type=url`)
- 승인 publisher adapter: `https://khan.co.kr/article/<숫자>`와 `https://www.khan.co.kr/article/<숫자>`
- 안전장치: HTTPS·host·port·redirect(최대 3)·공인 DNS IP·2 MiB HTML 상한을 모두 확인한다.
- 추출 계약: 기존 closure의 `KHAN_ARTICLE_V1` parser를 사용하며 제목·발행일·본문 및 raw HTML/article-text SHA-256 receipt를 반환한다.
- 그 뒤 frontend가 기존 `POST /api/v1/verify/develop`에 추출 본문·제목·발행일을 보내므로, 기존 L1~L5 → release-bound retrieval → unique `QUERY_READY` → KOSIS Cell API 경계를 유지한다.

## 이미지 입력

- 공개 API: `POST /api/v1/analyze/image`
- 허용: PNG, JPEG, WebP / 10 MiB / 40 Mpx / 긴 변 2240px로 정규화
- OCR: server-side CLOVA General OCR, 한국어, 요청당 1회
- 비밀값: `CLOVA_OCR_INVOKE_URL`, `CLOVA_OCR_SECRET`은 server-only `runtime.env`에만 둔다. browser·Git·API 응답에는 넣지 않는다.
- OCR text·image SHA-256·크기·OCR 상태만 출처 메타데이터로 남기며 원본 이미지 바이트는 응답하지 않는다.
- OCR 후에는 추출 본문을 기존 `verify_article_develop`로 직접 넘긴다. 상대 시점이 있으면 기존 checkpoint/추가 질문 흐름을 사용한다.

## 검증 결과

| 검사 | 결과 |
|---|---|
| Python syntax (`app`, gates, URL, image, OCR client) | 통과 |
| API Docker build | 통과 |
| Frontend production build | 통과 (Vite 32 modules) |
| URL adapter smoke | 통과: `202606242055015`에서 날짜 `2026-06-24`, 본문 756자 |
| URL API TestClient | 통과: supported URL 200, unsupported `example.com` 422 |
| Public HTTPS URL API | 통과: `https://news-verify.52.25.84.163.nip.io/api/v1/analyze` 200 |
| Public URL → existing verification API | 통과: 추출한 제목·발행일·본문을 `/api/v1/verify/develop`에 전달해 HTTP 200 |
| valid PNG image API | OCR 설정 부재를 정확히 `OCR_NOT_CONFIGURED` 503으로 반환 |
| DB/index/vector write | 0건 |
| 재시작 | API·Nginx만 재생성, BGE encoder 및 data services 재시작 없음 |

## 이미지 활성화 전 남은 외부 설정

현재 EC2에는 CLOVA Studio key만 있고, CLOVA OCR은 별도 상품의 API Gateway Invoke URL과 `X-OCR-SECRET`이 필요하다. NCP 콘솔에서 CLOVA OCR General 도메인과 API Gateway 연동을 만든 뒤 다음 두 값만 root-owned `runtime.env`에 추가한다.

```dotenv
CLOVA_OCR_INVOKE_URL=https://... # CLOVA OCR API Gateway Invoke URL
CLOVA_OCR_SECRET=...             # X-OCR-SECRET
```

그 후 API만 재생성하여 실제 이미지 E2E(한 장의 기사 캡처 → OCR text → 최종 검증 결과)를 실행한다. 이 설정 전에는 이미지 요청을 성공한 것처럼 보이지 않고 `OCR_NOT_CONFIGURED`에서 멈춘다.
