const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const TOKEN_KEY = "kosis-token";

export class ApiError extends Error {
  constructor(message, { status, code, detail } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function authToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

// 로그인 상태면 Authorization 헤더를 붙인다.
function authHeaders(extra = {}) {
  const token = authToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : { message: await response.text() };

  if (!response.ok) {
    throw new ApiError(
      payload.message || `백엔드 요청에 실패했습니다. (HTTP ${response.status})`,
      {
        status: response.status,
        code: payload.error_code,
        detail: payload.detail,
      },
    );
  }
  return payload;
}

// ── 검증 (로그인 시 계정에 저장, conversationId로 대화 이어가기) ──────────
export async function analyzeInput(
  text,
  { conversationId, inputType = "auto", focusQuestion = "" } = {},
) {
  const response = await fetch(`${API_BASE_URL}/v1/analyze`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      text,
      input_type: inputType,
      ...(focusQuestion ? { focus_question: focusQuestion } : {}),
      max_claims: 10,
      explain: false,
      ...(conversationId ? { conversation_id: conversationId } : {}),
    }),
  });
  return parseResponse(response);
}

// 기사 본문을 develop 배포 파이프라인(run_trace)으로 검증한다.
// 반환: { type:"article", status, live, summary, results:[segments], conversation_id? }
export async function verifyArticleDevelop(
  text,
  { conversationId, title = "", date = "" } = {},
) {
  const response = await fetch(`${API_BASE_URL}/v1/verify/develop`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      text,
      title,
      date,
      ...(conversationId ? { conversation_id: conversationId } : {}),
    }),
  });
  return parseResponse(response);
}

export async function analyzeImage(file, { conversationId, focusQuestion = "" } = {}) {
  const form = new FormData();
  form.append("file", file);
  if (conversationId) form.append("conversation_id", conversationId);
  if (focusQuestion) form.append("focus_question", focusQuestion);
  const response = await fetch(`${API_BASE_URL}/v1/analyze/image`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  return parseResponse(response);
}

export async function checkHealth() {
  const response = await fetch(`${API_BASE_URL}/health`);
  return parseResponse(response);
}

// ── 이메일 인증 (백엔드 실연동) ──────────────────────────
export async function registerApi(email, password, displayName) {
  const response = await fetch(`${API_BASE_URL}/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, display_name: displayName }),
  });
  return parseResponse(response); // { user, access_token, ... }
}

export async function loginApi(email, password) {
  const response = await fetch(`${API_BASE_URL}/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  return parseResponse(response); // { user, access_token, ... }
}

// ── 소셜 로그인 (백엔드 OAuth) ───────────────────────────
export function startKakaoLogin() {
  window.location.href = `${API_BASE_URL}/v1/auth/kakao/login`;
}

export function startNaverLogin() {
  window.location.href = `${API_BASE_URL}/v1/auth/naver/login`;
}

export function startGoogleLogin() {
  window.location.href = `${API_BASE_URL}/v1/auth/google/login`;
}

export async function authMe(token) {
  const response = await fetch(`${API_BASE_URL}/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return parseResponse(response); // { user: {...} }
}

export async function authLogout(token) {
  await fetch(`${API_BASE_URL}/v1/auth/logout`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
}

// ── 검증 기록 (대화) ─────────────────────────────────────
export async function listConversations({ limit = 30, offset = 0 } = {}) {
  const response = await fetch(
    `${API_BASE_URL}/v1/conversations?limit=${limit}&offset=${offset}`,
    { headers: authHeaders() },
  );
  return parseResponse(response); // { items|conversations, ... }
}

export async function getConversation(id) {
  const response = await fetch(`${API_BASE_URL}/v1/conversations/${id}`, {
    headers: authHeaders(),
  });
  return parseResponse(response); // { conversation, messages }
}

export async function deleteConversation(id) {
  const response = await fetch(`${API_BASE_URL}/v1/conversations/${id}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!response.ok && response.status !== 204) return parseResponse(response);
}

// ── 통계표 탐색 · 즐겨찾기 ────────────────────────────────
export async function searchTables(
  q = "",
  { limit = 30, offset = 0, organization = "" } = {},
) {
  const params = new URLSearchParams({ q, limit, offset });
  if (organization) params.set("org", organization);
  const response = await fetch(`${API_BASE_URL}/v1/tables?${params}`, {
    headers: authHeaders(), // 로그인 시 favorited 표시
  });
  return parseResponse(response); // { items, total, ... }
}

export async function listFavorites() {
  const response = await fetch(`${API_BASE_URL}/v1/favorites`, {
    headers: authHeaders(),
  });
  return parseResponse(response); // { items, total }
}

export async function addFavorite(tableKey) {
  const response = await fetch(`${API_BASE_URL}/v1/favorites`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ table_key: tableKey }),
  });
  return parseResponse(response);
}

export async function removeFavorite(tableKey) {
  const response = await fetch(
    `${API_BASE_URL}/v1/favorites/${encodeURIComponent(tableKey)}`,
    { method: "DELETE", headers: authHeaders() },
  );
  if (!response.ok && response.status !== 204) return parseResponse(response);
}
