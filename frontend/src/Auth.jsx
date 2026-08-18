import { useState, useEffect, useRef } from "react";
import "./Auth.css";

// ── 데모용 mock 인증 ─────────────────────────────────────
// 실제 인증 서버가 붙기 전까지 localStorage로 흉내낸다.
// 백엔드 연결 시 loadUsers/saveUsers/submit 안의 로직만 fetch("/api/auth/..")로 교체.
const USERS_KEY = "kosis_users"; // 가입한 계정 목록(데모)

// ── Google 로그인 (Google Identity Services) ─────────────
// 공개 값이라 프론트에 노출돼도 안전한 OAuth Client ID.
// frontend/.env.local 에  VITE_GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com  로 설정.
const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID;

// 구글이 준 ID 토큰(JWT)에서 프로필(이름/이메일) 추출 (UTF-8 안전)
function decodeJwt(token) {
  const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  const json = decodeURIComponent(
    atob(b64)
      .split("")
      .map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
      .join("")
  );
  return JSON.parse(json);
}

function GoogleSignIn({ onAuthed }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!GOOGLE_CLIENT_ID) return;
    let cancelled = false;

    function init() {
      if (cancelled || !window.google?.accounts?.id || !ref.current) return;
      window.google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: (resp) => {
          try {
            const p = decodeJwt(resp.credential);
            onAuthed({
              name: p.name || p.email,
              email: p.email,
              picture: p.picture,
              provider: "google",
            });
          } catch {
            /* 토큰 파싱 실패 시 무시 */
          }
        },
      });
      window.google.accounts.id.renderButton(ref.current, {
        theme: "outline",
        size: "large",
        text: "continue_with",
        shape: "pill",
        width: 300,
        locale: "ko",
      });
    }

    // GIS 스크립트 1회 로드
    if (window.google?.accounts?.id) {
      init();
    } else {
      let s = document.getElementById("gsi-script");
      if (s) {
        s.addEventListener("load", init);
      } else {
        s = document.createElement("script");
        s.id = "gsi-script";
        s.src = "https://accounts.google.com/gsi/client";
        s.async = true;
        s.defer = true;
        s.onload = init;
        document.head.appendChild(s);
      }
    }
    return () => {
      cancelled = true;
    };
  }, [onAuthed]);

  if (!GOOGLE_CLIENT_ID) {
    return (
      <p className="auth-google-hint">
        Google 로그인을 쓰려면 <code>VITE_GOOGLE_CLIENT_ID</code> 를 설정하세요.
      </p>
    );
  }
  return <div className="auth-google" ref={ref} />;
}

function loadUsers() {
  try {
    return JSON.parse(localStorage.getItem(USERS_KEY)) || [];
  } catch {
    return [];
  }
}
function saveUsers(users) {
  localStorage.setItem(USERS_KEY, JSON.stringify(users));
}

function AuthModal({ onClose, onAuthed }) {
  const [mode, setMode] = useState("login"); // "login" | "signup"
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [error, setError] = useState("");

  const isSignup = mode === "signup";

  function switchMode(next) {
    setMode(next);
    setError("");
    setPw("");
    setPw2("");
  }

  function submit(e) {
    e.preventDefault();
    setError("");

    const mail = email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(mail)) {
      setError("이메일 형식을 확인해 주세요.");
      return;
    }
    if (pw.length < 6) {
      setError("비밀번호는 6자 이상이어야 해요.");
      return;
    }

    const users = loadUsers();

    if (isSignup) {
      if (!name.trim()) {
        setError("이름을 입력해 주세요.");
        return;
      }
      if (pw !== pw2) {
        setError("비밀번호가 서로 달라요.");
        return;
      }
      if (users.some((u) => u.email === mail)) {
        setError("이미 가입된 이메일이에요. 로그인해 주세요.");
        return;
      }
      const user = { name: name.trim(), email: mail, password: pw };
      saveUsers([...users, user]);
      onAuthed({ name: user.name, email: user.email });
    } else {
      const found = users.find((u) => u.email === mail && u.password === pw);
      if (!found) {
        setError("이메일 또는 비밀번호가 일치하지 않아요.");
        return;
      }
      onAuthed({ name: found.name, email: found.email });
    }
  }

  return (
    <div className="auth-overlay" onClick={onClose}>
      <div
        className="auth-modal"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <button className="auth-close" onClick={onClose} aria-label="닫기">
          ×
        </button>

        <div className="auth-head">
          <span className="auth-logo">✓</span>
          <h2 className="auth-title">{isSignup ? "회원가입" : "로그인"}</h2>
          <p className="auth-sub">KOSIS 뉴스 수치 검증</p>
        </div>

        {/* 소셜 로그인 (Google) */}
        <div className="auth-social">
          <GoogleSignIn onAuthed={onAuthed} />
        </div>
        <div className="auth-divider">
          <span>또는 이메일로</span>
        </div>

        <form className="auth-form" onSubmit={submit}>
          {isSignup && (
            <label className="auth-field">
              <span>이름</span>
              <input
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
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
            />
          </label>
          <label className="auth-field">
            <span>비밀번호</span>
            <input
              type="password"
              value={pw}
              onChange={(e) => setPw(e.target.value)}
              placeholder="6자 이상"
              autoComplete={isSignup ? "new-password" : "current-password"}
            />
          </label>
          {isSignup && (
            <label className="auth-field">
              <span>비밀번호 확인</span>
              <input
                type="password"
                value={pw2}
                onChange={(e) => setPw2(e.target.value)}
                placeholder="다시 입력"
                autoComplete="new-password"
              />
            </label>
          )}

          {error && <div className="auth-error">{error}</div>}

          <button type="submit" className="auth-submit">
            {isSignup ? "가입하고 시작하기" : "로그인"}
          </button>
        </form>

        <div className="auth-switch">
          {isSignup ? (
            <>
              이미 계정이 있으세요?{" "}
              <button onClick={() => switchMode("login")}>로그인</button>
            </>
          ) : (
            <>
              처음이신가요?{" "}
              <button onClick={() => switchMode("signup")}>회원가입</button>
            </>
          )}
        </div>

        <p className="auth-note">
          ※ 데모용 임시 로그인입니다. 입력한 정보는 이 브라우저에만 저장돼요.
        </p>
      </div>
    </div>
  );
}

export default AuthModal;
