import { useEffect, useRef, useState } from "react";
import { useAuth } from "./auth.jsx";
import { LogoMark } from "./icons.jsx";
import { startKakaoLogin, startNaverLogin } from "./api.js";
import "./Login.css";

function onlyDigits(s) {
  return (s || "").replace(/\D/g, "");
}
function isValidPhone(s) {
  return /^01[016789]\d{7,8}$/.test(onlyDigits(s));
}
function gen6() {
  return String(Math.floor(100000 + Math.random() * 900000));
}

// 로그인 / 회원가입 모달 (프론트 목업 + 휴대폰 본인인증 목업)
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

function Login({ onClose }) {
  const { login, register } = useAuth();
  const [mode, setMode] = useState("login"); // login | signup
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const firstRef = useRef(null);

  // ── 휴대폰 본인인증(목업) 상태 ──
  const [phone, setPhone] = useState("");
  const [sentCode, setSentCode] = useState(null); // 발송된(목업) 인증번호
  const [codeInput, setCodeInput] = useState("");
  const [phoneVerified, setPhoneVerified] = useState(false);
  const [phoneMsg, setPhoneMsg] = useState("");

  useEffect(() => {
    firstRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isSignup = mode === "signup";

  function resetPhone() {
    setPhone("");
    setSentCode(null);
    setCodeInput("");
    setPhoneVerified(false);
    setPhoneMsg("");
  }

  function sendCode() {
    setPhoneMsg("");
    if (!isValidPhone(phone)) {
      setPhoneMsg("올바른 휴대폰 번호를 입력해 주세요.");
      return;
    }
    const code = gen6();
    setSentCode(code);
    setCodeInput("");
    setPhoneVerified(false);
    // 데모 환경: 실제로는 SMS 발송. 여기선 화면에 인증번호를 보여줍니다.
    setPhoneMsg(`데모용 인증번호 [${code}] — 실서비스에선 문자로 전송됩니다.`);
  }

  function verifyCode() {
    if (onlyDigits(codeInput) === sentCode) {
      setPhoneVerified(true);
      setPhoneMsg("");
    } else {
      setPhoneMsg("인증번호가 일치하지 않습니다.");
    }
  }

  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    try {
      if (isSignup) {
        if (name.trim().length < 1) throw new Error("이름을 입력해 주세요.");
        if (!phoneVerified) throw new Error("휴대폰 본인인증을 완료해 주세요.");
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
    resetPhone();
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
              ? "휴대폰 본인인증 후 계정을 만들 수 있어요."
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

          {isSignup && (
            <div className="auth-field">
              <span>
                휴대폰 본인인증
                {phoneVerified && <em className="auth-verified">✓ 인증 완료</em>}
              </span>
              <div className="auth-inline">
                <input
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="010-1234-5678"
                  autoComplete="tel"
                  disabled={phoneVerified}
                />
                <button
                  type="button"
                  className="auth-inline-btn"
                  onClick={sendCode}
                  disabled={phoneVerified}
                >
                  {sentCode ? "재전송" : "인증번호 전송"}
                </button>
              </div>

              {sentCode && !phoneVerified && (
                <div className="auth-inline">
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={6}
                    value={codeInput}
                    onChange={(e) => setCodeInput(e.target.value)}
                    placeholder="인증번호 6자리"
                  />
                  <button
                    type="button"
                    className="auth-inline-btn"
                    onClick={verifyCode}
                  >
                    확인
                  </button>
                </div>
              )}

              {phoneMsg && <p className="auth-hint">{phoneMsg}</p>}
            </div>
          )}

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

          <button
            className="auth-submit"
            type="submit"
            disabled={busy || (isSignup && !phoneVerified)}
          >
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
          데모용 로그인 · 본인인증은 목업이며 입력 정보는 이 브라우저에만
          저장됩니다.
        </p>
      </div>
    </div>
  );
}

export default Login;
