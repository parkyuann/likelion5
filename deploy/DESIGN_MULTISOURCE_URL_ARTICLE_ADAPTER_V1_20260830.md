# 다매체 URL 기사 입력 어댑터 v1 — 2026-08-30

## 목적

브라우저에서 받은 기사 URL을 본문·제목·발행일과 함께 `ArticleDocument`로만 변환한다.
이 문서는 URL 입력을 KOSIS 검색·후보 선택·Late Binding·Cell API 검증과 분리한다.

```text
URL
  -> HTTPS/DNS/redirect/size/content-type guard
  -> publisher adapter or guarded generic fallback
  -> provenance-bearing ArticleDocument
  -> existing article-text verification pipeline
```

따라서 URL 어댑터는 주장 추출, 통계표 검색, Qdrant/OpenSearch 쓰기, 재색인, 재임베딩,
KOSIS Cell API 호출을 수행하지 않는다. 이후 단계의 `QUERY_READY`와 deterministic verdict
계약도 변경하지 않는다.

## 지원 우선순위

| 구분 | 어댑터 | 본문 원천 |
| --- | --- | --- |
| 경향신문 | `KHAN_ARTICLE_V1` | 발행사 본문 DOM selector |
| 조선일보 | `CHOSUN_ARTICLE_V1` | `Fusion.globalContent.content_elements[type=text]` |
| 중앙일보 | `JOONGANG_ARTICLE_V1` | 발행사 본문 DOM selector |
| 동아일보 | `DONGA_ARTICLE_V1` | 발행사 본문 DOM selector |
| 네이버뉴스 | `NAVER_NEWS_ARTICLE_V1` | 발행사 본문 DOM selector와 게시시각 요소 |
| 그 외 공개 HTTPS 기사 | `GENERIC_ARTICLE_V1` | 제한된 HTML selector 또는 `NewsArticle.articleBody` |

전용 어댑터는 해당 발행사의 현재 서버 HTML 구조를 우선 사용한다. 구조가 바뀌어 본문·제목·날짜의
품질 조건을 충족하지 못하면 일반 본문이나 메뉴를 추측해 통과시키지 않고 `URL_EXTRACTION_FAILED`로
종료한다. 범용 보조 경로도 동일하다.

## 입력 안전성과 품질 조건

- `https`와 기본 HTTPS 포트만 허용한다.
- IP 리터럴, 자격증명이 포함된 URL, 비공개·로컬 DNS 대상, 과도한 redirect는 거부한다.
- HTML만 받고 응답 크기는 2 MiB, redirect는 최대 3회로 제한한다.
- 본문은 메뉴·광고·추천·댓글 영역을 제거한 뒤 최소 100자와 30개 이상의 한글/영문 글자 조건을
  충족해야 한다.
- receipt에는 원 URL·최종 URL·원 HTML SHA-256·본문 SHA-256·어댑터·선택자·문단별 SHA-256만
  남기며 원 HTML과 원문 전체를 지속 저장하지 않는다.

## 검증 및 반영 순서

1. 독립 Git branch에서 synthetic adapter test와 현재 공개 기사 표본을 실행한다.
2. 검증된 commit만 `develop`에 fast-forward로 반영한다.
3. EC2에서는 해당 Git SHA의 별도 worktree와 별도 Compose project/port로 API·Nginx shadow를
   기동한다. `deploy/compose.shadow-api-nginx.yaml`은 shadow 전용 내부 network를 만들고 기존 내부
   BGE encoder network만 external로 연결한다. 따라서 shadow API의 `api` alias는 shadow Nginx에서만
   보이며, 메인 Nginx와 이름 충돌하지 않는다. shadow에서는 `shadow-api shadow-nginx`만
   `--no-deps`로 기동해 GPU encoder를 중복 생성하지 않는다. 서버의 root-owned runtime 환경 파일은
   `SHADOW_RUNTIME_ENV_FILE`로 읽기 전용 주입한다. 기존 PostgreSQL·OpenSearch·Qdrant·Redis 컨테이너·
   volume은 읽기 전용으로 재사용하며 생성·삭제·재적재하지 않는다.
4. shadow에서 다매체 URL 입력과 기존 article-text pipeline 회귀를 실행하고, container·network·port
   충돌과 data-layer 기준선 변경이 없음을 확인한다.
5. 위 결과가 모두 통과한 동일 SHA만 main application overlay에 반영한다.

## 비범위

- URL 기사 자체의 진실성 판정 또는 원문 저작권 보증
- 로그인·Redis session/cache·BGE 모델·reranker 변경
- OpenSearch/Qdrant/PostgreSQL schema·index·collection·vector 변경
- 이미지/OCR 입력 또는 기존 `CLAIM_QUERY_TARGET_NOT_FOUND` 원인 수정
