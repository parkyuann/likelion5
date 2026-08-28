# backend — 뉴스 사실검증 API

기존 KOSIS 검증 파이프라인을 웹에서 호출할 수 있게 감싼 최소 FastAPI 서버.
개념·로드맵은 [`../docs/BACKEND_GUIDE.md`](../docs/BACKEND_GUIDE.md) 를 먼저 읽으세요.

## 파일 구성
- `app.py` — FastAPI 앱(엔드포인트 정의)
- `pipeline_service.py` — 기존 파이프라인을 부르는 얇은 래퍼

## 준비 (한 번만)

FastAPI 설치:

```bash
./.venv/Scripts/python.exe -m pip install "fastapi>=0.110"
```

## 실행 전 체크리스트 (중요)
서버가 답을 내려면 파이프라인의 의존성이 켜져 있어야 합니다.
1. 로컬 **Qdrant** 서버 실행 (`http://127.0.0.1:6333`, 컬렉션 `kosis_tables_v5`)
2. 루트 `.env` 에 `NCP_CLOVASTUDIO_API_KEY`, `KOSIS_API_KEY` 존재
3. 아래 명령은 **반드시 `./.venv`** 파이썬으로 실행

## 서버 켜기 (저장소 루트에서)

```bash
./.venv/Scripts/python.exe -m uvicorn backend.app:app --reload --port 8000
```

켜지면 브라우저에서 **http://localhost:8000/docs** 를 열고, 각 엔드포인트의
"Try it out" 버튼으로 프론트 없이 바로 시험할 수 있습니다.

## 엔드포인트

### 인증과 대화 기록

현재 인증은 이메일·비밀번호 방식입니다. 비밀번호는 PBKDF2-SHA256으로 해시하고,
로그인 토큰은 원문 대신 SHA-256 해시만 SQLite에 저장합니다. 기본 DB 파일은
`backend_data/likelion5.db`이며 `BACKEND_DB_PATH`로 변경할 수 있습니다.

회원가입은 로그인 토큰까지 함께 반환합니다.

```http
POST /v1/auth/register
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "8자 이상의 비밀번호",
  "display_name": "사용자"
}
```

로그인과 현재 사용자 확인:

```http
POST /v1/auth/login
GET  /v1/auth/me
POST /v1/auth/logout
Authorization: Bearer l5_발급받은토큰
```

대화 기록 API는 로그인이 필수입니다.

```http
POST   /v1/conversations
GET    /v1/conversations?limit=20&offset=0
GET    /v1/conversations/{conversation_id}
DELETE /v1/conversations/{conversation_id}
```

로그인한 상태에서 `/v1/analyze`를 호출하면 사용자 입력과 백엔드 결과가 자동으로
저장됩니다. `conversation_id`를 생략하면 새 대화를 만들고 응답에 생성된 ID를
추가합니다. 다음 요청에 이 ID를 전달하면 같은 대화에 이어서 저장됩니다.

```json
{
  "text": "2025년 인구는 몇 명인가요?",
  "input_type": "auto",
  "conversation_id": "이전 응답에서 받은 ID"
}
```

이미지 분석은 multipart 폼의 `conversation_id` 필드로 기존 대화를 이어갑니다.
이미지 바이너리는 DB에 저장하지 않고 파일명·MIME과 OCR 결과만 대화 기록에
보존합니다.

### `POST /v1/analyze` — 입력 라우팅 통합 진입점

간단한 통계 질문은 KOSIS MCP로 전달합니다. 기사 URL과 직접 입력 본문은
전체 텍스트를 보존한 `ArticleDocument`로 만들고 `ready_for_verification` 상태로
반환합니다. 팀의 고도화 기사 분석 프로세스가 호출 가능한 함수로 확정되면 이
문서 객체 전체를 해당 함수에 전달합니다.

```json
{
  "text": "2025년 취업자 수는 몇 명인가요?",
  "input_type": "auto",
  "max_claims": 10,
  "explain": false
}
```

`input_type`은 `auto`, `query`, `url`, `article` 중 하나입니다. 명시적 유형이
자동 판별보다 우선합니다.

이미지는 JSON 본문 대신 multipart 업로드가 필요하므로 별도 통합 경로를 사용합니다.

### `POST /v1/analyze/image` — 이미지 OCR

Swagger에서 `Try it out`을 누른 뒤 PNG, JPEG 또는 WebP 파일을 선택해 실행합니다.
최대 파일 크기는 기본 10MiB이며 `OCR_MAX_UPLOAD_BYTES`로 변경할 수 있습니다.
이미지의 전체 OCR 결과는 다른 기사 입력과 동일하게
`article_document.text`에 들어갑니다. OCR 원문은 `extraction.raw_text`에도
보존됩니다.

```bash
curl -X POST http://localhost:8000/v1/analyze/image \
  -F "file=@article.png"
```

OCR은 `.env`의 `NCP_CLOVASTUDIO_API_KEY`(또는 `HCX_API_KEY`)를 사용하며,
기본 모델은 `HCX-005`입니다. 모델·엔드포인트·타임아웃은 각각
`HCX_VISION_MODEL`, `HCX_VISION_ENDPOINT`, `HCX_VISION_TIMEOUT_SECONDS`로
설정할 수 있습니다.

KOSIS MCP 연결 환경변수:

```dotenv
KOSIS_MCP_URL=https://your-kosis-mcp.example/mcp
KOSIS_MCP_TOOL=query_kosis
KOSIS_MCP_QUESTION_ARGUMENT=question
# 인증이 필요한 서버만 설정
KOSIS_MCP_TOKEN=
```

현재 서버는 Streamable HTTP MCP의 `initialize → tools/list → tools/call` 순서로
호출합니다. 실제 KOSIS MCP 서버의 도구명과 질문 인자명이 다르면 위 두 환경변수를
맞춰야 합니다.

기사 URL 예시:

```json
{
  "text": "https://news.example.com/article/123",
  "input_type": "url",
  "max_claims": 10
}
```

URL은 SSRF 검사를 거쳐 본문과 메타데이터를 추출합니다. `/v1/analyze`는 기존
문장 단위 `verify_article()`을 호출하지 않으며, 응답의
`article_document.text`에 추출된 전체 본문을 그대로 담습니다.

```json
{
  "type": "article_document",
  "status": "ready_for_verification",
  "article_document": {
    "source_type": "url",
    "text": "추출된 기사 전체 본문..."
  }
}
```

### `POST /v1/verify/develop` — 기사 본문 검증 (정본)
검증은 `src/develop` 파이프라인으로 일원화되었습니다. (레거시 `/verify/claim`·`/verify/article`은 제거됨)
요청:
```json
{ "text": "기사 본문 전체...", "title": "제목", "date": "2024-01-15" }
```
응답의 `results` 는 문장별 세그먼트(검증 대상은 `verdict`·`answer` 포함) 목록입니다.
간단한 통계 질문(예: "2024년 청년 실업률은 얼마인가요?")은 `/v1/analyze`가 KOSIS MCP로 라우팅합니다.

### `GET /health` — 서버 생존 확인
`{ "status": "ok" }` 를 돌려줍니다. 프론트 연결 테스트에 사용하세요.

## 프론트에서 호출 예시
```js
const res = await fetch("http://localhost:8000/v1/verify/develop", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text: "기사 본문 전체..." }),
});
const data = await res.json();
```

## 참고
- 기사 전체 검증은 주장 수만큼 LLM·검색·KOSIS 호출이 일어나 **수십 초** 걸릴 수 있습니다.
  데모 땐 짧은 기사 또는 `max_claims` 를 작게 두세요.
