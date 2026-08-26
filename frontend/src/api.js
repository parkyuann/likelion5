const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(message, { status, code, detail } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function apiUrl(path) {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

async function apiFetch(path, init = {}) {
  const headers = new Headers(init.headers || {});
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(apiUrl(path), { ...init, headers, credentials: "include" });
}

async function parseResponse(response) {
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : { message: await response.text() };
  if (!response.ok) {
    const message = payload?.message || `요청을 처리할 수 없습니다. (HTTP ${response.status})`;
    throw new ApiError(message, {
      status: response.status,
      code: payload?.code,
      detail: payload?.detail,
    });
  }
  return payload;
}

export async function analyzeInput(text, { conversationId, inputType = "auto", focusQuestion = "" } = {}) {
  const response = await apiFetch("/v1/analyze", {
    method: "POST",
    body: JSON.stringify({
      text, input_type: inputType,
      ...(focusQuestion ? { focus_question: focusQuestion } : {}),
      max_claims: 10, explain: false,
      ...(conversationId ? { conversation_id: conversationId } : {}),
    }),
  });
  return parseResponse(response);
}

export async function verifyArticleDevelop(text, { conversationId, title = "", date = "" } = {}) {
  const response = await apiFetch("/v1/verify/develop", {
    method: "POST",
    body: JSON.stringify({ text, title, date, ...(conversationId ? { conversation_id: conversationId } : {}) }),
  });
  return parseResponse(response);
}

export async function analyzeImage(file, { conversationId, focusQuestion = "" } = {}) {
  const form = new FormData();
  form.append("file", file);
  if (conversationId) form.append("conversation_id", conversationId);
  if (focusQuestion) form.append("focus_question", focusQuestion);
  const response = await apiFetch("/v1/analyze/image", { method: "POST", body: form });
  return parseResponse(response);
}

export async function checkHealth() {
  return parseResponse(await fetch("/health", { credentials: "include" }));
}

export async function registerApi(email, password, displayName) {
  return parseResponse(await apiFetch("/auth/signup", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name: displayName }),
  }));
}

export async function loginApi(email, password) {
  return parseResponse(await apiFetch("/auth/login", {
    method: "POST", body: JSON.stringify({ email, password }),
  }));
}

export async function authMe() {
  return parseResponse(await apiFetch("/auth/me"));
}

export async function authLogout() {
  return parseResponse(await apiFetch("/auth/logout", { method: "POST" }));
}

export async function authLogoutAll() {
  return parseResponse(await apiFetch("/auth/logout-all", { method: "POST" }));
}

export async function listConversations({ limit = 30, offset = 0 } = {}) {
  const params = new URLSearchParams({ limit, offset });
  return parseResponse(await apiFetch(`/v1/conversations?${params}`));
}

export async function getConversation(id) {
  return parseResponse(await apiFetch(`/v1/conversations/${encodeURIComponent(id)}`));
}

export async function deleteConversation(id) {
  const response = await apiFetch(`/v1/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!response.ok && response.status !== 204) return parseResponse(response);
  return null;
}

export async function searchTables(q = "", { limit = 30, offset = 0, organization = "" } = {}) {
  const params = new URLSearchParams({ q, limit, offset });
  if (organization) params.set("org", organization);
  return parseResponse(await apiFetch(`/v1/tables?${params}`));
}

export async function listFavorites() {
  return parseResponse(await apiFetch("/v1/favorites"));
}

export async function addFavorite(tableKey) {
  return parseResponse(await apiFetch("/v1/favorites", {
    method: "POST", body: JSON.stringify({ table_key: tableKey }),
  }));
}

export async function removeFavorite(tableKey) {
  const response = await apiFetch(`/v1/favorites/${encodeURIComponent(tableKey)}`, { method: "DELETE" });
  if (!response.ok && response.status !== 204) return parseResponse(response);
  return null;
}
