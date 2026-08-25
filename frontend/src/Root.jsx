import { useState } from "react";
import Landing from "./Landing.jsx";
import ChatApp from "./ChatApp.jsx";
import Explore from "./Explore.jsx";
import History from "./History.jsx";
import AuthModal from "./Auth.jsx";
import {
  IconPlus,
  IconNewspaper,
  IconStar,
  IconTable,
  IconUser,
  IconSettings,
} from "./icons.jsx";
import "./Root.css";

const CURRENT_USER_KEY = "kosis_current_user";

// 전체 레이아웃: 왼쪽 사이드바(접기/펼치기) + 본문(랜딩/챗봇/탐색/기록)
function Root() {
  const [article, setArticle] = useState(null); // null = 랜딩(입력) 화면
  const [view, setView] = useState("home"); // "home" | "explore" | "history"
  const [collapsed, setCollapsed] = useState(false); // 사이드바 접힘
  const [authOpen, setAuthOpen] = useState(false);
  const [user, setUser] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(CURRENT_USER_KEY)) || null;
    } catch {
      return null;
    }
  });

  function goHome() {
    setArticle(null);
    setView("home");
  }
  function handleAuthed(u) {
    setUser(u);
    localStorage.setItem(CURRENT_USER_KEY, JSON.stringify(u));
    setAuthOpen(false);
  }
  function logout() {
    setUser(null);
    localStorage.removeItem(CURRENT_USER_KEY);
  }

  return (
    <div className="shell">
      <div className="shell-body">
        {/* 왼쪽 사이드바 (브랜드 · 검색 · 로그인 모두 여기로) */}
        <aside className={`sidebar ${collapsed ? "collapsed" : ""}`}>
          {/* 상단: 브랜드 + 접기/펼치기 토글 (같은 줄, 높이 맞춤) */}
          <div className="sidebar-head">
            {/* 브랜드: 로고 + 제목 + 배지 (클릭 시 새 검증) */}
            <div
              className="sidebar-brand"
              onClick={goHome}
              role="button"
              tabIndex={0}
              title="새 검증"
            >
              <div className="sidebar-brand-text">
                <span className="sidebar-title">KOSIS 뉴스 수치 검증</span>
                <span className="sidebar-badge">국가통계 팩트체크</span>
              </div>
            </div>

            <button
              className="sidebar-toggle"
              onClick={() => setCollapsed((c) => !c)}
              title={collapsed ? "펼치기" : "접기"}
              aria-label={collapsed ? "사이드바 펼치기" : "사이드바 접기"}
            >
              {collapsed ? "»" : "«"}
            </button>
          </div>

          <button className="sidebar-new" onClick={goHome} title="새 검증">
            <span className="si-icon"><IconPlus /></span>
            <span className="si-label">새 검증</span>
          </button>
          <nav className="sidebar-nav">
            <button
              className={`sidebar-item ${view === "history" ? "active" : ""}`}
              onClick={() => setView("history")}
              title="검증 기록"
            >
              <span className="si-icon"><IconNewspaper /></span>
              <span className="si-label">검증 기록</span>
            </button>
            <button className="sidebar-item" disabled title="즐겨찾기">
              <span className="si-icon"><IconStar /></span>
              <span className="si-label">즐겨찾기</span>
            </button>
            <button
              className={`sidebar-item ${view === "explore" ? "active" : ""}`}
              onClick={() => setView("explore")}
              title="통계표 탐색"
            >
              <span className="si-icon"><IconTable /></span>
              <span className="si-label">통계표 탐색</span>
            </button>
          </nav>
          <div className="sidebar-foot">
            {/* 로그인 / 사용자 (상단 헤더에서 이동) */}
            {user ? (
              <div className="sidebar-user" title={`${user.name}님`}>
                <span className="si-icon"><IconUser /></span>
                <span className="sidebar-username si-label">{user.name}님</span>
                <button
                  className="sidebar-auth ghost si-label"
                  onClick={logout}
                >
                  로그아웃
                </button>
              </div>
            ) : (
              <button
                className="sidebar-auth"
                onClick={() => setAuthOpen(true)}
                title="로그인"
              >
                <span className="si-icon"><IconUser /></span>
                <span className="si-label">로그인</span>
              </button>
            )}
            <button className="sidebar-item" disabled title="설정">
              <span className="si-icon"><IconSettings /></span>
              <span className="si-label">설정</span>
            </button>
            <span className="sidebar-note si-label">일부 기능 준비 중</span>
          </div>
        </aside>

        {/* 본문 */}
        <main className="shell-main">
          {view === "explore" ? (
            <Explore />
          ) : view === "history" ? (
            <History />
          ) : article === null ? (
            <Landing onSubmit={(text) => setArticle(text)} />
          ) : (
            <ChatApp initialArticle={article} />
          )}
        </main>
      </div>

      {authOpen && (
        <AuthModal onClose={() => setAuthOpen(false)} onAuthed={handleAuthed} />
      )}
    </div>
  );
}

export default Root;
