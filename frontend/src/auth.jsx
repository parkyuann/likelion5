import { createContext, useContext, useEffect, useState } from "react";
import { authMe, authLogout, loginApi, registerApi } from "./api.js";

/* Cookie-only authentication; no browser user snapshot or client credential. */
const AuthContext = createContext(null);

function backendUser(user) {
  return {
    id: user.id,
    name: user.display_name,
    email: user.primary_email || "",
    provider: "email",
    status: user.status,
    createdAt: user.created_at,
    lastLoginAt: user.last_login_at,
    backend: true,
  };
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);

  useEffect(() => {
    let alive = true;
    authMe()
      .then((response) => {
        if (alive && response?.user) setUser(backendUser(response.user));
      })
      .catch(() => alive && setUser(null));
    return () => { alive = false; };
  }, []);

  async function login(email, password) {
    const response = await loginApi(email, password);
    setUser(backendUser(response.user));
  }

  async function register(name, email, password) {
    // Signup creates an account only; a separate login issues the session.
    return registerApi(email, password, name);
  }

  async function logout() {
    try {
      await authLogout();
    } finally {
      setUser(null);
    }
  }

  return <AuthContext.Provider value={{ user, login, register, logout }}>
    {children}
  </AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
