import { useEffect, useRef, useState } from "react";
import { useAuth } from "./auth.jsx";
import { LogoMark } from "./icons.jsx";
import { startKakaoLogin, startNaverLogin, startGoogleLogin } from "./api.js";
import "./Login.css";

// 로그인 / 회원가입 모달 (이메일 + 소셜: 카카오·네이버·구글)
function KakaoMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 3C6.5 3 2 6.6 2 11c0 2.8 1.85 5.26 4.6 6.68-.2.72-.73 2.65-.83 3.06-.13.5.18.5.39.36.16-.11 2.6-1.77 3.66-2.5.71.1 1.44.15 2.18.15 5.5 0 10-3.6 10-8S17.5 3 12 3z" />
    </svg>
  );
}
function NaverMark() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M15.3 12.55 8.43 2H2v20h6.7V11.44L15.57 22H22V2h-6.7z" />
    </svg>
  );
}
function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
      <path fill="#4285F4" d="M23.52 12.27c0-.82-.07-1.6-.2-2.36H12v4.47h6.47a5.53 5.53 0 0 1-2.4 3.63v3h3.88c2.27-2.09 3.57-5.17 3.57-8.74z" />
      <path fill="#34A853" d="M12 24c3.24 0 5.96-1.08 7.95-2.91l-3.88-3.01c-1.08.72-2.45 1.15-4.07 1.15-3.13 0-5.78-2.11-6.73-4.96H1.26v3.11A12 12 0 0 0 12 24z" />
      <path fill="#FBBC05" d="M5.27 14.27a7.2 7.2 0 0 1 0-4.54V6.62H1.26a12 12 0 0 0 0 10.76z" />
      <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.44-3.44A11.96 11.96 0 0 0 12 0 12 12 0 0 0 1.26 6.62l4.01 3.11C6.22 6.86 8.87 4.75 12 4.75z" />
    </svg>
  );
}

function Login({ onClose }) {
  const { login, register } = useAuth();
  const [mode, setMode] = useState("login"); // login | signup
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const firstRef = useRef(null);

  useEffect(() => {
    firstRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isSignup = mode === "signup";

  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    try {
      if (isSignup) {
        if (name.trim().length < 1) throw new Error("이름을 입력해 주세요.");
        if (password.length < 8)
          throw new Error("비밀번호는 8자 이상이어야 합니다.");
        setBusy(true);
        await register(name.trim(), email.trim(), password);
      } else {
        setBusy(true);
        await login(email.trim(), password);
      }
      onClose();
    } catch (err) {
      setError(err.message || "요청을 처리할 수 없습니다.");
    } finally {
      setBusy(false);
    }
  }

  function switchMode() {
    setMode(isSignup ? "login" : "signup");
    setError("");
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal auth-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={isSignup ? "회원가입" : "로그인"}
      >
        <button className="modal-close" onClick={onClose} aria-label="닫기">
          ✕
        </button>

        <div className="auth-head">
          <span className="auth-logo" aria-hidden="true">
            <LogoMark size={44} />
          </span>
          <h2 className="auth-title">
            {isSignup ? "회원가입" : "다시 오신 걸 환영해요"}
          </h2>
          <p className="auth-sub">
            {isSignup
              ? "이메일로 간편하게 가입하거나 소셜 로그인을 이용하세요."
              : "로그인하고 검증 기록을 이어가세요."}
          </p>
        </div>

        <div className="auth-social">
          <button
            type="button"
            className="social-btn kakao"
            onClick={startKakaoLogin}
          >
            <KakaoMark />
            카카오로 계속하기
          </button>
          <button
            type="button"
            className="social-btn naver"
            onClick={startNaverLogin}
          >
            <NaverMark />
            네이버로 계속하기
          </button>
          <button
            type="button"
            className="social-btn google"
            onClick={startGoogleLogin}
          >
            <GoogleMark />
            구글로 계속하기
          </button>
        </div>
        <p className="auth-social-hint">
          기존 계정은 로그인되고, 처음 이용하는 계정은 회원가입됩니다.
        </p>

        <div className="auth-divider">
          <span>또는 이메일로</span>
        </div>

        <form className="auth-form" onSubmit={submit}>
          {isSignup && (
            <label className="auth-field">
              <span>이름</span>
              <input
                ref={firstRef}
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="홍길동"
                autoComplete="name"
              />
            </label>
          )}

          <label className="auth-field">
            <span>이메일</span>
            <input
              ref={isSignup ? undefined : firstRef}
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              required
            />
          </label>

          <label className="auth-field">
            <span>비밀번호</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete={isSignup ? "new-password" : "current-password"}
              required
            />
          </label>

          {error && <p className="auth-error">{error}</p>}

          <button className="auth-submit" type="submit" disabled={busy}>
            {busy ? "처리 중…" : isSignup ? "회원가입" : "로그인"}
          </button>
        </form>

        <p className="auth-switch">
          {isSignup ? "이미 계정이 있으신가요?" : "아직 계정이 없으신가요?"}{" "}
          <button type="button" className="auth-switch-btn" onClick={switchMode}>
            {isSignup ? "로그인" : "회원가입"}
          </button>
        </p>

        <p className="auth-note">
          데모용 로그인 · 입력 정보는 이 브라우저에만 저장됩니다.
        </p>
      </div>
    </div>
  );
}

export default Login;
