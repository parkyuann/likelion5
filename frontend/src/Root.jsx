import { useState, useEffect } from "react";
import Intro from "./Intro.jsx";
import Landing from "./Landing.jsx";
import ChatApp from "./ChatApp.jsx";
import Login from "./Login.jsx";
import TableExplorer from "./TableExplorer.jsx";
import {
  checkHealth,
  listConversations,
  getConversation,
  deleteConversation,
} from "./api.js";
import { useAuth } from "./auth.jsx";
import {
  SunIcon,
  MoonIcon,
  ClockIcon,
  StarIcon,
  ChartIcon,
  LogoutIcon,
  ComposeIcon,
  LogoMark,
} from "./icons.jsx";
import "./Root.css";

const THEME_KEY = "kosis-theme";
const GUEST_HISTORY_KEY = "kosis-guest-conversations";

function readGuestConversations() {
  try {
    const parsed = JSON.parse(localStorage.getItem(GUEST_HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function guestTitle(payload) {
  const source = payload?.mock?.input?.display || payload?.text || payload?.image?.name || "새 검증";
  const normalized = String(source).replace(/\s+/g, " ").trim();
  return normalized.length > 34 ? `${normalized.slice(0, 34)}…` : normalized;
}

function serializableGuestMessages(messages) {
  return messages.map((message) => {
    if (message.kind === "image") {
      return {
        role: "user",
        kind: "text",
        text: `첨부 이미지: ${message.name || "이미지"}`,
      };
    }
    if (message.kind === "document" && message.document?.text) {
      return {
        ...message,
        document: {
          ...message.document,
          text: message.document.text.slice(0, 30000),
        },
      };
    }
    return message;
  });
}

// 라이트/다크 테마: 명시 선택 시 localStorage 저장 + data-theme 강제, 미선택 시 시스템 설정
function useTheme() {
  const [choice, setChoice] = useState(() => {
    if (typeof localStorage === "undefined") return null;
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : null;
  });
  const [sysDark, setSysDark] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
  );

  useEffect(() => {
    const root = document.documentElement;
    if (choice) root.setAttribute("data-theme", choice);
    else root.removeAttribute("data-theme");
  }, [choice]);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e) => setSysDark(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const isDark = choice ? choice === "dark" : sysDark;
  const toggle = () => {
    const next = isDark ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    setChoice(next);
  };
  return { isDark, toggle };
}

// 백엔드 헬스 상태 (마운트 시 1회)
function useHealth() {
  const [status, setStatus] = useState("checking");
  useEffect(() => {
    let alive = true;
    checkHealth()
      .then(() => alive && setStatus("ok"))
      .catch(() => alive && setStatus("down"));
    return () => {
      alive = false;
    };
  }, []);
  return status;
}

const HEALTH_LABEL = {
  checking: "서버 확인 중",
  ok: "연결됨",
  down: "연결 안 됨",
};

const SIDEBAR_KEY = "kosis-sidebar";

// 사이드바 열림/접힘 상태 (localStorage 저장, 첫 방문은 넓은 화면에서만 열림)
function useSidebar() {
  const [open, setOpen] = useState(() => {
    if (typeof window === "undefined") return true;
    const saved = localStorage.getItem(SIDEBAR_KEY);
    if (saved === "open") return true;
    if (saved === "closed") return false;
    return false; // 처음 시작 시엔 사이드바 숨김 (로고 클릭으로 열기)
  });
  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, open ? "open" : "closed");
  }, [open]);
  return [open, () => setOpen((v) => !v), () => setOpen(false)];
}

function initials(name) {
  return (name || "?").trim().charAt(0).toUpperCase();
}

function Root() {
  const [session, setSession] = useState(null); // { input } | null → 랜딩
  const { isDark, toggle } = useTheme();
  const health = useHealth();
  const { user, logout } = useAuth();
  const [showLogin, setShowLogin] = useState(false);
  const [explorer, setExplorer] = useState(null); // null | "search" | "favorites"
  const [intro, setIntro] = useState(true); // 첫 로드 시 로고 인트로 재생
  const [sidebarOpen, toggleSidebar, closeSidebar] = useSidebar();
  const [chatScrolled, setChatScrolled] = useState(false); // 챗 스크롤 시 서비스명 숨김

  // 검증 기록(대화 목록)
  const [conversations, setConversations] = useState([]);
  const [guestConversations, setGuestConversations] = useState(
    readGuestConversations
  );
  const [historyTick, setHistoryTick] = useState(0);
  useEffect(() => {
    if (!user?.backend) {
      setConversations([]);
      return;
    }
    let alive = true;
    listConversations()
      .then((r) => alive && setConversations(r.items || []))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [user, historyTick]);

  useEffect(() => {
    localStorage.setItem(
      GUEST_HISTORY_KEY,
      JSON.stringify(guestConversations.slice(0, 30))
    );
  }, [guestConversations]);

  // 화면 전환(랜딩↔챗, 다른 대화 열기) 시 서비스명 노출 초기화
  useEffect(() => {
    setChatScrolled(false);
  }, [session?.key]);

  // input: { text } | { image: File } 형태의 초기 검증 요청
  function startVerify(payload) {
    if (user?.backend) {
      setSession({ payload, key: Date.now() });
      return;
    }
    const guestId = globalThis.crypto?.randomUUID?.() || `guest-${Date.now()}`;
    const now = new Date().toISOString();
    setGuestConversations((prev) => [
      {
        id: guestId,
        title: guestTitle(payload),
        messages: [],
        updated_at: now,
      },
      ...prev,
    ]);
    setSession({ payload, guestId, key: Date.now() });
  }
  // 새 검증: 입력 화면으로
  function goHome() {
    setSession(null);
  }
  // 검증 기록에서 대화 열기
  async function openConversation(id) {
    try {
      const conv = await getConversation(id);
      setSession({ conversationId: id, messages: conv.messages, key: Date.now() });
    } catch {
      /* 무시 */
    }
  }
  function openGuestConversation(item) {
    setSession({
      guestId: item.id,
      displayMessages: item.messages,
      key: Date.now(),
    });
  }
  function updateGuestConversation(id, messages) {
    setGuestConversations((prev) =>
      prev.map((item) =>
        item.id === id
          ? {
              ...item,
              messages: serializableGuestMessages(messages),
              updated_at: new Date().toISOString(),
            }
          : item
      )
    );
  }
  async function removeConversation(id, e) {
    e.stopPropagation();
    if (!user?.backend) {
      setGuestConversations((prev) => prev.filter((item) => item.id !== id));
      if (session?.guestId === id) setSession(null);
      return;
    }
    try {
      await deleteConversation(id);
    } catch {
      /* 무시 */
    }
    setHistoryTick((t) => t + 1);
    if (session?.conversationId === id) setSession(null);
  }

  return (
    <div className="shell">
      {/* 페이지 좌상단 로고 (plani 스타일) — 클릭 시 사이드바 열림/닫힘 */}
      <div className="page-head">
        <button
          className={`page-brand ${
            chatScrolled && !sidebarOpen ? "brand-hidden" : ""
          }`}
          onClick={toggleSidebar}
          aria-expanded={sidebarOpen}
          aria-label={sidebarOpen ? "사이드바 접기" : "사이드바 펼치기"}
          title={sidebarOpen ? "사이드바 접기" : "사이드바 펼치기"}
        >
          <span className="brand-logo" aria-hidden="true">
            <LogoMark size={28} />
          </span>
          <span className="brand-title">
            KOSIS <span className="brand-title-accent">팩트체크</span>
          </span>
        </button>
      </div>

      <div className="shell-body">
        <div
          className={`sidebar-backdrop ${sidebarOpen ? "show" : ""}`}
          onClick={closeSidebar}
        />
        <aside className={`sidebar ${sidebarOpen ? "" : "collapsed"}`}>
          <div className="sidebar-inner">
          <div className="sidebar-section">
            <nav className="sidebar-nav">
              <button className="sidebar-item" onClick={goHome}>
                <span className="sidebar-item-icon" aria-hidden="true">
                  <ComposeIcon />
                </span>
                새 검증
              </button>
              <button
                className="sidebar-item"
                onClick={() => setExplorer("favorites")}
              >
                <span className="sidebar-item-icon" aria-hidden="true">
                  <StarIcon />
                </span>
                즐겨찾기
              </button>
              <button
                className="sidebar-item"
                onClick={() => setExplorer("search")}
              >
                <span className="sidebar-item-icon" aria-hidden="true">
                  <ChartIcon />
                </span>
                통계표 탐색
              </button>
            </nav>
          </div>

          <div className="sidebar-section sidebar-history">
              <span className="sidebar-heading">
                <ClockIcon size="0.95em" /> 검증 기록
              </span>
              {(user?.backend ? conversations : guestConversations).length === 0 ? (
                <p className="sidebar-empty">아직 검증 기록이 없어요.</p>
              ) : (
                <div className="history-list">
                  {(user?.backend ? conversations : guestConversations).map((c) => (
                    <div
                      key={c.id}
                      className={`history-item ${
                        (user?.backend
                          ? session?.conversationId
                          : session?.guestId) === c.id
                          ? "active"
                          : ""
                      }`}
                      onClick={() =>
                        user?.backend
                          ? openConversation(c.id)
                          : openGuestConversation(c)
                      }
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) =>
                        (e.key === "Enter" || e.key === " ") &&
                        (user?.backend
                          ? openConversation(c.id)
                          : openGuestConversation(c))
                      }
                    >
                      <span className="history-title">{c.title}</span>
                      <button
                        className="history-del"
                        onClick={(e) => removeConversation(c.id, e)}
                        title="삭제"
                        aria-label="검증 기록 삭제"
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

          <div className="sidebar-foot">
            <div className="sidebar-utils">
              <span
                className={`health health-${health}`}
                title={`백엔드 ${HEALTH_LABEL[health]}`}
              >
                <span className="health-dot" aria-hidden="true" />
                <span className="health-text">{HEALTH_LABEL[health]}</span>
              </span>
            </div>

            {user ? (
              <div className="account-row">
                <span className="user-avatar">{initials(user.name)}</span>
                <div className="account-id">
                  <strong>{user.name}</strong>
                  <span>{user.email}</span>
                </div>
                <button
                  className="account-logout"
                  onClick={logout}
                  title="로그아웃"
                  aria-label="로그아웃"
                >
                  <LogoutIcon />
                </button>
              </div>
            ) : (
              <div className="login-promo">
                <strong>기록을 계정에 보관하세요</strong>
                <p>
                  비로그인 기록은 이 브라우저에 저장됩니다. 로그인하면 다른
                  기기에서도 기록과 즐겨찾기를 확인할 수 있어요.
                </p>
                <button
                  className="login-cta"
                  onClick={() => setShowLogin(true)}
                >
                  로그인
                </button>
              </div>
            )}
          </div>
          </div>
        </aside>

        <main
          className={`shell-main ${session === null ? "" : "chat-mode"}`}
          onPointerMove={(e) => {
            if (session !== null) return;
            const rect = e.currentTarget.getBoundingClientRect();
            e.currentTarget.style.setProperty(
              "--grid-x",
              `${e.clientX - rect.left}px`
            );
            e.currentTarget.style.setProperty(
              "--grid-y",
              `${e.clientY - rect.top}px`
            );
          }}
          onPointerLeave={(e) => {
            if (session !== null) return;
            e.currentTarget.style.setProperty("--grid-x", "50%");
            e.currentTarget.style.setProperty("--grid-y", "42%");
          }}
        >
          <div className="bg-layer" aria-hidden="true">
            <span className="data-grid" />
          </div>
          {session === null ? (
            <Landing onSubmit={startVerify} />
          ) : (
            <ChatApp
              key={session.key}
              initial={session.payload}
              initialMessages={session.messages}
              initialDisplayMessages={session.displayMessages}
              initialConversationId={session.conversationId}
              onSaved={() => setHistoryTick((t) => t + 1)}
              onScroll={setChatScrolled}
              onMessagesChange={(messages) => {
                if (session.guestId) {
                  updateGuestConversation(session.guestId, messages);
                }
              }}
            />
          )}
        </main>
      </div>

      {/* 우측 하단 테마 토글 (반투명 원형) */}
      <button
        className="theme-fab"
        onClick={toggle}
        title={isDark ? "라이트 모드로" : "다크 모드로"}
        aria-label={isDark ? "라이트 모드로 전환" : "다크 모드로 전환"}
      >
        {isDark ? <SunIcon size="1.3em" /> : <MoonIcon size="1.3em" />}
      </button>

      {intro && <Intro onDone={() => setIntro(false)} />}

      {explorer && (
        <TableExplorer
          initialTab={explorer}
          user={user}
          onClose={() => setExplorer(null)}
          onRequireLogin={() => {
            setExplorer(null);
            setShowLogin(true);
          }}
        />
      )}

      {showLogin && <Login onClose={() => setShowLogin(false)} />}
    </div>
  );
}

export default Root;
