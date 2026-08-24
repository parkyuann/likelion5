import { createContext, useContext, useEffect, useState } from "react";
import { authMe, authLogout, loginApi, registerApi } from "./api.js";

/*
 * 인증 컨텍스트.
 * - 이메일·카카오·네이버: 모두 백엔드 세션(Bearer 토큰, TOKEN_KEY) 기반의 실제 로그인.
 *   소셜은 OAuth 콜백이 ?access_token=&provider= 를 붙여 프론트로 복귀 → 아래 마운트 효과가 처리.
 */

const SESSION_KEY = "kosis-session"; // UI 복원용 사용자 스냅샷(빠른 표시)
const TOKEN_KEY = "kosis-token"; // 백엔드 Bearer 토큰

const AuthContext = createContext(null);

function readSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function backendUser(user, provider) {
  return {
    name: user.display_name,
    email: user.email,
    provider,
    backend: true,
  };
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => readSession());

  useEffect(() => {
    if (user) localStorage.setItem(SESSION_KEY, JSON.stringify(user));
    else localStorage.removeItem(SESSION_KEY);
  }, [user]);

  // 카카오 콜백 복귀 + 저장된 토큰으로 세션 복원
  useEffect(() => {
    const url = new URL(window.location.href);
    const incoming = url.searchParams.get("access_token");
    const provider = url.searchParams.get("provider") || "kakao";
    if (incoming) {
      localStorage.setItem(TOKEN_KEY, incoming);
      url.searchParams.delete("access_token");
      url.searchParams.delete("provider");
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    }
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) {
      authMe(token)
        .then((r) => setUser(backendUser(r.user, provider)))
        .catch(() => localStorage.removeItem(TOKEN_KEY));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 이메일 로그인 (백엔드)
  async function login(email, password) {
    const res = await loginApi(email, password);
    localStorage.setItem(TOKEN_KEY, res.access_token);
    setUser(backendUser(res.user, "email"));
  }

  // 이메일 회원가입 (백엔드). phone은 프론트 본인인증(목업) 통과 여부 확인용.
  async function register(name, email, password /* , phone */) {
    const res = await registerApi(email, password, name);
    localStorage.setItem(TOKEN_KEY, res.access_token);
    setUser(backendUser(res.user, "email"));
  }

  function logout() {
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) {
      authLogout(token).catch(() => {});
      localStorage.removeItem(TOKEN_KEY);
    }
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
