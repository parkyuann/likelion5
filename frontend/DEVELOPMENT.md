# 프론트엔드 개발 가이드 — KOSIS 팩트체크

뉴스의 수치 주장을 국가통계(KOSIS)로 검증해 주는 서비스의 프론트엔드.
이 문서는 새 기능을 붙이거나 UI를 고칠 때 **매번 지켜야 할 원칙과 규칙**을 정리한 것이다.
(디자인 방향은 여러 번의 피드백으로 다듬어진 결과이므로, 이유를 이해하고 유지할 것.)

---

## 1. 기술 스택 · 구조

- **React 19 + Vite** (JavaScript, TypeScript 아님)
- 스타일: **CSS 커스텀 프로퍼티(디자인 토큰)** + 컴포넌트별 `.css`
- 상태: React 훅 + Context(`auth.jsx`). 상태관리 라이브러리 없음.
- 폰트: Pretendard(CDN, 실패 시 시스템 폰트 폴백)

### 파일 지도 (`frontend/src/`)

| 파일 | 역할 |
|---|---|
| `Root.jsx` / `Root.css` | 앱 셸: 좌상단 로고, 사이드바(검증 기록·즐겨찾기·통계표 탐색), 테마 FAB, 배경 blob, 인트로/로그인 마운트 |
| `Landing.jsx` / `.css` | 첫 입력 화면(히어로 + 입력 카드 + 예시 칩 + 이미지 첨부/드롭/붙여넣기) |
| `ChatApp.jsx` / `.css` | 검증 대화 화면: 말풍선, 결과 렌더, 로딩 인디케이터, 입력창 |
| `Intro.jsx` / `.css` | 첫 로드 로고 인트로 애니메이션 |
| `Login.jsx` / `.css` | 로그인/회원가입 모달(이메일 + 카카오/네이버 + 휴대폰 본인인증 목업) |
| `auth.jsx` | 인증 Context(백엔드 세션·토큰 관리) |
| `api.js` | 백엔드 호출 계층(모든 fetch는 여기서) |
| `icons.jsx` | 라인 SVG 아이콘 + `LogoMark` |

### 데이터 흐름
```
Landing(입력) ─▶ Root.startVerify ─▶ ChatApp(세션)
                                     │
                    api.js ─▶ 백엔드 /v1/analyze(+토큰) ─▶ 결과 렌더
                                     │
             로그인 시 대화 저장 ─▶ Root 검증 기록 목록 갱신(onSaved)
```

---

## 2. 디자인 원칙 (가장 중요 — 반복된 피드백)

> 한 줄 요약: **"AI가 대충 만든 티"를 내지 말고, 사용성을 최우선으로.**

1. **AI 티/개밤티 금지.** 밋밋한 플랫·형광 파스텔·과한 이모지·기계적인 레이아웃 지양.
   레퍼런스(plani.co.kr 등)의 **구성·톤을 참고**하되 그대로 복제하지 않는다.
2. **그라디언트는 유지한다.** 버튼·말풍선의 은은한 그라디언트/그림자가 "완성도" 신호다.
   (플랫하게 다 없앴다가 "그라디언트 있는 게 낫다"는 피드백을 받은 적 있음.)
3. **소프트 파스텔 팔레트(Pigment 톤).** 블루그레이 + 크림/베이지.
   - 블루: `#9CBBCB` / `#7699AD`, 웜: `#FFF2E5` / `#E0C9B5`
4. **움직임으로 생동감을.** 배경 blob은 실제로 크게 움직여야 한다(정적이면 "안 움직인다"는 피드백).
   전환·로딩에는 부드러운 애니메이션을 준다. 단, 과하지 않게.
5. **여백과 정렬.** 클린 SaaS 느낌. 요소 간격·정렬을 흐트러뜨리지 않는다.
6. **모던 레퍼런스 관성 유지.** 헤더 없는 ChatGPT/Linear 식 구성, 좌상단 로고, 우하단 테마 FAB.

---

## 3. 디자인 토큰 · 테마 (반드시 준수)

**색을 하드코딩하지 말고 `index.css`의 토큰(`var(--...)`)만 쓴다.** 새 색이 필요하면 토큰을 먼저 정의.

주요 토큰군:
- 배경/표면: `--bg`, `--bg-2`, `--surface`, `--surface-2/3`
- 텍스트: `--ink`, `--ink-soft`, `--muted`, `--faint`
- 선: `--line`, `--line-strong`
- 브랜드: `--brand`, `--brand-ink`, `--brand-strong`, `--brand-tint`, `--brand-tint-2`, `--brand-tint-line`
- 버튼: `--btn-top`, `--btn-bottom`(현재 플랫: 둘이 같음), `--on-accent`
- 웜 액센트: `--warm`, `--warm-tint`
- 판정: `--green*`(일치), `--red*`(불일치/오류)

### 3-상태 테마 규칙
테마는 **light / dark / system** 3가지다. 색은 반드시 아래 3곳에 정의:
1. 순수 `:root` — 라이트 팔레트(기본값)
2. `@media (prefers-color-scheme: dark)` — 시스템 다크
3. `:root[data-theme="dark"]` — 사용자가 토글로 명시한 다크

> 어떤 색도 미디어쿼리/`[data-theme]` **안에서만** 정의하지 말 것.
> `body`에는 항상 명시적인 토큰 배경을 준다.

- 다크 베이스는 **채도 높은 네이비 금지**(탁해 보임). 따뜻한 차콜 계열로.
- 다크에서 blob은 라이트보다 **연하게**.

---

## 4. 카피(문구) · UX 원칙

1. **사용자를 탓하지 않는다.** 잘못 입력해도 "의미 없는 입력" 같은 판정투 금지.
   무엇이 틀렸는지가 아니라 **무엇을 넣으면 되는지 + 예시**를 안내한다.
2. **오류 ≠ 안내를 시각적으로 구분한다.**
   - 진짜 오류(서버 연결 실패 등) → 빨간 말풍선 + ⚠ 아이콘(`.c-error`)
   - 범위 밖 입력(`OUT_OF_SCOPE`) → 부드러운 안내 말풍선(`.c-notice`, 브랜드 톤)
   - 판별 근거: `api.js`가 던지는 `ApiError.code`. `code === "OUT_OF_SCOPE"`면 `notice`.
3. **내부 용어를 노출하지 않는다.** "백엔드", 에러코드(`(OUT_OF_SCOPE)`), LLM 내부 판정 사유 등은 사용자에게 보이지 않게.
4. **기다림엔 진행감을 준다.** 정적 문장 대신 **움직이는 로딩 인디케이터**
   (바운싱 점 + 1.8초마다 순환하는 상태 문구 + shimmer). `ChatApp.jsx`의 `LOADING_STEPS` 참고.
5. **비활성 상태는 부드럽게.** 전송 버튼 disabled를 회색 블록 + `not-allowed`(🚫)로 하지 말고,
   흐린 브랜드 버튼 + 기본 커서로("금지"가 아니라 "아직 입력 전").
6. **입력 전엔 예시로 시작, 입력 시작하면 예시 숨김.**

---

## 5. 인증 모델 (`auth.jsx` + `api.js`)

- **이메일·카카오**: 백엔드 실제 세션. Bearer 토큰을 localStorage(`kosis-token`)에 저장.
- **네이버·휴대폰 본인인증**: 아직 **목업**(백엔드 토큰 없음 → 검증 저장 불가). 실연동 시 카카오처럼 OAuth 콜백으로 교체.
- 로그인 사용자만 `user.backend === true` → **검증 기록/즐겨찾기** 기능 노출.
- 토큰은 `api.js`의 `authHeaders()`가 모든 요청에 자동 첨부.

### localStorage 키
| 키 | 용도 |
|---|---|
| `kosis-token` | 백엔드 Bearer 토큰 |
| `kosis-session` | UI 복원용 사용자 스냅샷 |
| `kosis-theme` | `light`/`dark`(미설정 시 시스템) |
| `kosis-sidebar` | `open`/`closed` |

---

## 6. 백엔드 API 계약 (`api.js` 경유로만 호출)

기본 URL: `VITE_API_BASE_URL`(미설정 시 동일 오리진). 모든 fetch는 `api.js`에 두고 컴포넌트에서 직접 fetch 금지.

| 함수 | 엔드포인트 | 비고 |
|---|---|---|
| `analyzeInput(text,{conversationId})` | `POST /v1/analyze` | 텍스트 검증. `input_type:"auto"` |
| `analyzeImage(file,{conversationId})` | `POST /v1/analyze/image` | 이미지 OCR → 검증 |
| `registerApi` / `loginApi` | `POST /v1/auth/register` / `login` | `{user, access_token}` 반환 |
| `startKakaoLogin()` | `GET /v1/auth/kakao/login` | 리다이렉트, 콜백은 `?access_token=` |
| `authMe` / `authLogout` | `GET /v1/auth/me` / `POST logout` | 토큰 검증/폐기 |
| `listConversations` / `getConversation` / `deleteConversation` | `/v1/conversations…` | 검증 기록 |
| `checkHealth` | `GET /health` | 서버 연결 상태 배지 |

- 오류는 `ApiError(message, {status, code, detail})`로 던짐 → UI는 `message`만 표시(§4 참고).
- `analyze` 결과의 `type`(`simple_query`/`article_document`/…)에 따라 `ChatApp.pushResult`가 렌더 분기.
- **라우팅은 백엔드에서 LLM(HCX-005 function calling)이 판정**한다. 프론트는 항상 `input_type:"auto"`로 보내고 분류에 관여하지 않는다.

---

## 7. 접근성 · 반응형

- 모든 아이콘 버튼에 `aria-label`, 토글에 `aria-expanded`.
- 클릭 가능한 `div`는 `role="button"` + `tabIndex` + `onKeyDown`(Enter/Space).
- **`prefers-reduced-motion`**: 애니메이션(로딩 shimmer 등)은 이 설정에서 비활성.
- 넓은 콘텐츠(표·코드)는 가로 스크롤 컨테이너로 감싸고, 페이지 body가 가로로 넘치지 않게.
- 사이드바는 좁은 화면에서 backdrop + collapse.

---

## 8. 성능 · 주의점

- **LLM 라우팅 비용**: 텍스트 1건당 백엔드가 HCX 호출 1회(~2.5~3초). 명백한 잡담은 백엔드 사전필터로 0ms 처리되지만, 정상 질의는 지연이 있다 → 로딩 인디케이터(§4-4)가 중요.
- 이미지 미리보기 `URL.createObjectURL`은 **반드시 `revokeObjectURL`로 해제**(`clearPendingImage`).
- 대화 목록은 `historyTick`으로 갱신, 화면 전환 시 `session.key`로 `ChatApp` 리마운트.

---

## 9. 변경 전/후 체크리스트

- [ ] 색을 하드코딩하지 않고 토큰만 썼는가?
- [ ] 라이트·다크·시스템 3-상태 모두에서 확인했는가?
- [ ] 문구가 사용자를 탓하거나 내부 용어(백엔드/에러코드)를 노출하지 않는가?
- [ ] 오류와 안내를 시각적으로 올바르게 구분했는가?
- [ ] 로딩/비활성/호버 등 상태가 부드럽고 자연스러운가?
- [ ] 새 fetch는 `api.js`에 두었는가? 토큰이 자동 첨부되는가?
- [ ] `aria-label`/키보드 조작/`prefers-reduced-motion`을 챙겼는가?
- [ ] `npm run check`가 통과하는가?(린트·빌드 오류와 미사용 import 제거)

---

## 10. 실행

```bash
# 프론트(최초 설치는 frontend 디렉터리에서 npm ci)
cd frontend && npm run dev        # http://127.0.0.1:5173

# 백엔드(검증까지 하려면 로컬 Qdrant + .env 키 필요)
./.venv/Scripts/python.exe -m uvicorn backend.app:app --reload --port 8000
```
