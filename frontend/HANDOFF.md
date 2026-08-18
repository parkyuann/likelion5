# KOSIS 팩트체크 프론트엔드 — 인수인계(Handoff)

최종 갱신: 2026-08-18 · 브랜치: `hyeonseo`

> 이 문서만 읽으면 다른 세션에서도 이어서 개발할 수 있도록 **현재 구조 그대로** 정리했습니다.

## 1. 이게 뭔가요

뉴스 기사 속 **수치 주장**을 국가통계(KOSIS)와 대조해 검증하는 파이프라인의 **프론트엔드**입니다.
백엔드(파이썬 파이프라인)는 별도로 있고, 이 프로젝트는 결과를 보여주는 **웹 UI**만 담당합니다.

- **현재 상태**: 백엔드 연결 전. **mock(가짜) 데이터**로 화면·동작을 구현한 프로토타입.
- **스택**: React 19 + Vite (JavaScript, JSX). 외부 UI 라이브러리 없음. CSS 직접 작성.
- **폰트**: Pretendard(웹폰트, `index.css`에서 CDN import). 색/그림자/반경은 `index.css`의 CSS 변수(토큰).

## 2. 실행 방법

```bash
cd frontend
npm install   # 최초 1회
npm run dev    # 개발 서버 → http://localhost:5173
```

- 빌드: `npm run build` (결과물 `frontend/dist/`).
- 윈도우에서는 프로젝트 루트의 `프론트엔드_실행.bat` 더블클릭으로도 실행 가능.

## 3. 화면 흐름 (중요 — 예전 버전과 다름)

```
[Landing 첫 화면: 기사 입력]  ──검증하기──▶  [ChatApp 챗봇 화면]
   (가운데 히어로 + 큰 입력창)                 (첫 기사가 자동으로 첫 대화가 됨)
                                              이후 아래 입력창으로 계속 대화
```

- 상단에 **항상 고정 헤더**, 왼쪽에 **사이드바**(레이아웃 shell)가 감쌈.
- "새 검증"(사이드바) 또는 헤더 로고 클릭 → 다시 Landing으로.
- 사이드바 탭으로 본문 전환: **🕘 검증 기록**(History) · **📊 통계표 탐색**(KOSIS iframe). (⭐즐겨찾기·⚙️설정은 아직 자리표시)
- 헤더 우측 **로그인** → 모달. 로그인 후 `{이름}님 · 로그아웃` 표시(세션은 localStorage 유지, 재접속해도 유지).

## 4. 파일 구조 (현재)

| 파일 | 역할 |
| --- | --- |
| `src/main.jsx` | 진입점. `Root`를 렌더 (건드릴 일 거의 없음) |
| `src/Root.jsx` / `Root.css` | **레이아웃 shell**: 상단 헤더 + 왼쪽 사이드바 + 본문. `view`(`home`/`explore`/`history`)로 본문 분기. `home`에서 `article===null`이면 Landing, 아니면 ChatApp. 헤더 우측에 로그인/사용자 표시. |
| `src/Landing.jsx` / `Landing.css` | **첫 화면**(기사 입력). 제출 시 `onSubmit(text)` → Root가 ChatApp으로 전환 |
| `src/ChatApp.jsx` / `ChatApp.css` | ⭐ **핵심**. 챗봇 + 검증 흐름 + 결과 렌더 전부. `ArticleResult`를 export(검증 기록 상세에서 재사용), 검증 완료 시 `addHistory` 저장 |
| `src/Explore.jsx` / `Explore.css` | **통계표 탐색**. KOSIS(`index.do`)를 iframe으로 직접 임베드(0.8배 축소). |
| `src/Auth.jsx` / `Auth.css` | **로그인/회원가입 모달**(데모용 mock, localStorage) |
| `src/History.jsx` / `History.css` | **검증 기록** 목록·상세(예전 결과 카드 재현) |
| `src/history.js` | 검증 기록 저장소(localStorage). `loadHistory`/`addHistory`/`clearHistory` |
| `src/index.css` | 전역 토큰(색·그림자·반경), 폰트, body 배경 |
| `src/App.jsx` / `src/App.css` | **옛 단일화면 버전. 현재 흐름에서 안 씀(미사용).** 남겨만 둠 — 지워도 됨 |

> **인증/기록/탐색은 모두 백엔드 없는 프론트 mock**입니다(localStorage). 실제 서버 붙일 때 교체 지점은 각 파일 상단 주석 참고(`Auth.jsx`의 loadUsers/saveUsers/submit, `history.js`의 loadHistory/addHistory).

> 주의: `App.jsx`는 더 이상 렌더되지 않습니다. 예전 문서/코드가 `App.jsx`의 `handleVerify`, `setLogs`, `splitSentences`를 언급하면 그건 **구버전**입니다. 실제 로직은 전부 `ChatApp.jsx`에 있습니다.

## 5. ChatApp.jsx 핵심 구조

**컴포넌트/함수 (파일 상단부터)**
- `kosisTableUrl(orgId, tblId)` — KOSIS 표 URL 조립: `https://kosis.kr/statHtml/statHtml.do?orgId=..&tblId=..`
- `STAGES` — 진행 단계 문구(사용자 친화적): "문장을 분석하는 중이에요" 등 5단계
- `VERDICTS` — 판정 4종 라벨/색 클래스 매핑
- `DEMO_CASES` — mock 판정 시나리오 4개(match/mismatch/notfound/outofscope)
- `splitSentences(text)` — 문장 분리. `(?<=[.!?。\n])(?!\d)` 로 소수점(예 5.2) 오분리 방지
- `hasNumber(s)` — 숫자 포함 여부(검증 대상 판별)
- `mockAnalyze(article)` — 기사를 문장 배열로 → 숫자 든 문장에 DEMO_CASES 순서대로 배정
- `Candidates` — "고려한 통계표 후보 N개" 접기/펼치기
- `ClaimDetail` — 문장 클릭 시 뜨는 상세(판정/답변/표연산/표링크/후보)
- `ArticleResult({segments})` — 결과 말풍선: 요약 건수 + 기사 전체 + 클릭 판정
- `UserArticleBubble({text})` — 사용자 기사 말풍선(길면 3줄 접기 + "원문 펼치기")
- `calcTargetPercent(logs, totalSent)` — 진행률 목표치(%) 계산
- `ProgressBubble({logs, nowTs, startTs, totalMs, done, pct})` — 진행 말풍선(문장별 시간 + 전체 시간 + 원형 % 링)
- `ChatApp({initialArticle})` — 메인. 상태·타이머·렌더

**ChatApp 내부 상태**
- `messages` — 대화 배열. 각 원소 `kind`: `"text"`(사용자 기사/봇 안내) · `"progress"`(진행 말풍선) · `"article"`(결과)
- `input` — 하단 입력창 값
- `loading` — 검증 중 여부
- `sentLogs` — 문장별 진행 `{n,total,status:'pending'|'running'|'done',stage,stageIdx,verdict,durMs,startTs}`
- `nowTs`/`pct` — 실시간 시계 / 표시 진행률
- refs: `startAllRef, timerRef(시계), animRef(퍼센트 카운트업), pctRef, targetPctRef, startedRef(중복 자동실행 방지), bottomRef`

**동작 흐름 (`runVerify(rawText)`)**
1. 사용자 메시지 push
2. `mockAnalyze` → 검증 대상 문장 수 안내 메시지 push (0개면 결과만 보여주고 종료)
3. 타이머(시계) + 퍼센트 카운트업 인터벌 시작
4. 문장마다: pending→running(STAGES 한 줄씩 교체)→done. `sync()`가 `sentLogs`와 진행률 목표 갱신
5. 완료: 타이머 정리, `pct=100`, **progress 메시지(진행기록 보존) + article 메시지(결과)** 를 push
6. `handleSend()`는 하단 입력창용, `initialArticle`은 Landing에서 넘어온 첫 기사를 `useEffect`로 자동 검증(startedRef로 1회만)

## 6. 판정 종류 (4가지)

| verdict | 의미 | 색 |
| --- | --- | --- |
| `match` | 일치 | 초록 |
| `mismatch` | 불일치 | 빨강 |
| `notfound` | 검증 불가능 · 매칭 실패(후보는 있으나 지표 매칭 실패) | 회색 |
| `outofscope` | 검증 불가능 · 대상 밖(전망·개별사례 등, 사전 분류 제외) | 회색 |

## 7. 백엔드 연결 시 (가장 중요)

`ChatApp.jsx`의 `runVerify()`에서 **두 부분만** 실제 API로 교체하면 됩니다.

### (A) 검증 요청 → 결과 받기
`mockAnalyze(text)`를 실제 `fetch` 결과로 교체. 프론트가 기대하는 **문장별 결과 배열** 계약:

```json
[
  {
    "text": "...23만 명으로 집계됐다.",
    "verifiable": true,
    "verdict": "match",
    "answer": "통계청 ... 일치합니다.",
    "calc": "(a − b) ÷ b × 100 = +3.93%",
    "table": { "name": "...", "orgId": "101", "tblId": "DT_104Y260", "path": "통계청 › ... › 2024년" },
    "candidates": [
      { "rank": 1, "key": "101:DT_104Y260", "name": "...", "score": 6.0, "status": "선택" },
      { "rank": 2, "key": "301:DT_200Y134", "name": "...", "score": 2.0, "status": "지표없음" }
    ]
  },
  { "text": "정부는 ...", "verifiable": false }
]
```
- `calc`/`table`/`candidates`는 없으면 `null`.
- `candidates[].key`는 `"orgId:tblId"` 형식(프론트가 분리해 링크 생성).
- 백엔드가 문장 분리를 하면 프론트의 `splitSentences`는 안 써도 됨(배열 그대로 렌더).

### (B) 실시간 진행
지금은 mock이 `STAGES`를 흉내냄. 실제는 백엔드가 단계를 **스트리밍**(SSE/WebSocket)으로 보내면 `sentLogs`를 갱신하도록 연결. 스트리밍이 부담이면 폴링도 가능.

### API 서버(별도 작업, 아직 없음)
파이썬 파이프라인을 **FastAPI** 등으로 감싸 `POST /verify` 제공. 개발 중 CORS는 Vite 프록시(`vite.config.js`) 또는 `fetch("http://localhost:8000/verify")`로 처리.

## 7-1. 이번 세션 추가 기능 (로그인 · 검증 기록 · 통계표 탐색)

전부 **프론트 mock**(localStorage). 백엔드 붙일 때 각 파일 상단 주석의 지점만 fetch로 교체.

### 로그인 / 회원가입 (`Auth.jsx`)
- 헤더 우측 `로그인` → 모달(로그인 ↔ 회원가입 토글).
- 검증: 이메일 형식 · 비밀번호 6자 이상 · 비밀번호 확인 일치 · 중복 이메일 차단.
- 저장: `localStorage["kosis_users"]`(가입 목록), `localStorage["kosis_current_user"]`(현재 세션). Root가 세션을 읽어 로그인 상태 유지.
- ⚠️ **데모용**: 비밀번호가 평문으로 localStorage에 저장됨. 실서비스는 반드시 서버 인증(해싱·토큰)으로 교체.

### 검증 기록 (`History.jsx` + `history.js`)
- ChatApp이 검증을 끝낼 때마다 `addHistory({article, segments})` 저장(최근 50건, 최신순, `localStorage["kosis_history"]`).
- 사이드바 `🕘 검증 기록` → 목록(기사 요약·날짜·일치/불일치/검증불가 건수) → 항목 클릭 → 상세(`ArticleResult`로 예전 결과 카드 그대로 재현, 문장 클릭 근거까지). `전체 삭제` 제공.
- 계정과는 분리(브라우저 단위). 계정별로 묶으려면 백엔드 연결 때 `session/user` 키로.

### 통계표 탐색 (`Explore.jsx`)
- 사이드바 `📊 통계표 탐색` → 본문에 KOSIS(`https://kosis.kr/index/index.do`)를 **iframe으로 직접 임베드**.
- 임베드 가능 근거: `X-Frame-Options`/CSP `frame-ancestors`/프레임 탈출 스크립트 **모두 없음**(실측: iframe load 정상, top이 우리 앱 유지). 단 KOSIS 이용약관상 허용 여부는 별도 확인 권장.
- 배율: `.explore-frame`에 `transform: scale(0.8)`(width/height 125%로 보정해 컨테이너 꽉 채움). 값만 바꾸면 배율 조절.

## 8. 다음 할 일 후보

- [ ] **입력 다형화**: 기사원문 외에 **질의(질문)·이미지·URL**도 받기
  - 질의 → 결과를 "기사 하이라이트"가 아닌 **단일 답변 카드**(`ClaimDetail`/`Candidates` 재사용)로 분기
  - URL → 링크 감지 + "기사 불러오는 중" 단계 + 본문추출 후 기사 흐름
  - 이미지 → 📎첨부/드래그/붙여넣기 + 썸네일 + "이미지에서 글자 읽는 중" 단계(OCR/비전)
  - 권장: **통합 입력창 하나**가 텍스트/URL 자동감지 + 이미지 첨부를 다 받게
- [ ] FastAPI로 파이프라인 래핑(`POST /verify`)
- [ ] `runVerify`의 mock → 실제 fetch 교체(위 계약)
- [ ] 진행 스트리밍(SSE/WebSocket) 연결
- [ ] 에러/실패 처리 UI
- [ ] 배포(Vercel 등, 백엔드 연결 후)

## 9. 알아두면 좋은 함정 / 이미 해결한 것

- **문장 분리 소수점 버그**: `5.2%`를 문장 끝으로 오인 → `splitSentences`의 `(?!\d)`로 해결.
- **자동 스크롤 튐**: 예전엔 진행 갱신마다 맨 아래로 튐 → 자동 스크롤 `useEffect` deps를 `[messages, loading]`로 제한(진행 중 문장 갱신엔 스크롤 안 함).
- **퍼센트 카운트업 vs 백그라운드 탭**: 미리보기/탭이 비활성이면 `setTimeout`이 스로틀됨. 마지막 구간은 즉시 `pct=100`으로 처리해 멈춘 것처럼 보이는 문제 방지.
- **채팅 폭 흔들림**: 진행 말풍선은 `.progress-bubble { width: 500px }`로 고정(내용 길이에 따라 안 흔들리게).
- **개발 중 콘솔 경고** `useEffect changed size`: hook deps 배열을 편집하면 Vite Fast Refresh가 남기는 **일시적 경고**. 하드 리로드하면 사라짐 — 실제 버그 아님.
- **디자인 방향**: 과거 "미니멀 에디토리얼(세리프·이모지 제거)" 시도는 **되돌렸음**. 현재는 네이비(`--brand: #23324f`) + Pretendard. 이모지 아이콘 유지.
- **여백**: "위아래가 좁다" 피드백 반영 — 랜딩/채팅/기록 화면의 상하 패딩을 키움(`.landing` 48px, `.chat-body` 44px, `.history-*` 28~32px).
- **개발 포트(멀티 인스턴스)**: 여러 세션이 동시에 dev 서버를 띄우면 5173 충돌 → `.claude/launch.json`에 `"autoPort": true`, `vite.config.js`가 `process.env.PORT`를 읽도록 함(지정 시 그 포트로 바인딩, 없으면 기본 5173). 단독 실행엔 영향 없음.
