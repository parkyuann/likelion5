# KOSIS 팩트체크 프론트엔드

KOSIS 국가통계와 기사·질문의 수치를 대조하는 React 프론트엔드입니다.
FastAPI 백엔드의 통합 분석 API를 사용합니다.

## 기술 구성

- React 19
- Vite 8
- JavaScript(ES modules)
- Oxlint
- 별도 UI·상태 관리 라이브러리 없음

## 개발 요구사항

- Node.js `20.19 이상` 또는 `22.12 이상` (`.nvmrc` 권장 버전: `22.20.0`)
- npm `10 이상`
- API 기능 확인 시 `http://127.0.0.1:8000`에서 실행 중인 백엔드

## 처음 실행하기

프로젝트 루트에서 백엔드를 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --reload --port 8000
```

새 터미널에서 프론트엔드를 설치하고 실행합니다.

```powershell
cd frontend
npm ci
npm run dev
```

브라우저에서 `http://127.0.0.1:5173`을 엽니다. 로컬 개발 중 `/v1`과
`/health` 요청은 Vite 프록시를 거쳐 `http://127.0.0.1:8000`으로 전달됩니다.

## 환경변수

필요할 때만 예시 파일을 복사합니다.

```powershell
Copy-Item .env.example .env.local
```

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | 빈 문자열 | 배포 환경의 백엔드 공개 URL. 비어 있으면 동일 오리진(Vite 프록시)을 사용합니다. |

`VITE_`로 시작하는 값은 브라우저 번들에 포함되므로 비밀키를 넣지 마세요.

## 명령어

| 명령 | 설명 |
| --- | --- |
| `npm run dev` | 개발 서버 실행(HMR) |
| `npm run lint` | Oxlint 정적 검사 |
| `npm run build` | 배포 번들을 `dist/`에 생성 |
| `npm run check` | 린트 후 프로덕션 빌드까지 한 번에 검증 |
| `npm run preview` | 생성된 배포 번들을 로컬에서 확인 |

의존성이 바뀌지 않은 일반 설치에는 잠금 파일을 그대로 따르는 `npm ci`를 사용합니다.
패키지를 추가하거나 갱신할 때만 `npm install <패키지>`를 사용하고
`package.json`과 `package-lock.json`을 함께 반영합니다.

## 연결 API

- `POST /v1/analyze` — 통계 질문, 기사 URL, 기사 본문 분석
- `POST /v1/analyze/image` — 이미지 OCR 분석
- `GET /health` — 백엔드 상태 확인

UI 구조와 구현 규칙은 `DEVELOPMENT.md`를 참고하세요. `HANDOFF.md`는 초기
프로토타입 시점의 기록이므로 현재 구현 기준으로 사용하지 않습니다.
