import { useState } from "react";
import Landing from "./Landing.jsx";
import ChatApp from "./ChatApp.jsx";
import "./Root.css";

// 전체 레이아웃: 상단 헤더(항상 표시) + 왼쪽 사이드바 + 본문(랜딩/챗봇)
function Root() {
  const [article, setArticle] = useState(null); // null = 랜딩(입력) 화면

  return (
    <div className="shell">
      {/* 항상 남아있는 상단 헤더 */}
      <header className="topbar">
        <div
          className="topbar-brand"
          onClick={() => setArticle(null)}
          role="button"
          tabIndex={0}
        >
          <span className="topbar-logo">✓</span>
          <span className="topbar-title">KOSIS 뉴스 수치 검증</span>
        </div>
        <span className="topbar-badge">국가통계 팩트체크</span>
      </header>

      <div className="shell-body">
        {/* 왼쪽 사이드바 (기능은 추후 추가) */}
        <aside className="sidebar">
          <button className="sidebar-new" onClick={() => setArticle(null)}>
            ＋ 새 검증
          </button>
          <nav className="sidebar-nav">
            <button className="sidebar-item" disabled>
              🕘 검증 기록
            </button>
            <button className="sidebar-item" disabled>
              ⭐ 즐겨찾기
            </button>
            <button className="sidebar-item" disabled>
              📊 통계표 탐색
            </button>
          </nav>
          <div className="sidebar-foot">
            <button className="sidebar-item" disabled>
              ⚙️ 설정
            </button>
            <span className="sidebar-note">기능 준비 중</span>
          </div>
        </aside>

        {/* 본문 */}
        <main className="shell-main">
          {article === null ? (
            <Landing onSubmit={(text) => setArticle(text)} />
          ) : (
            <ChatApp initialArticle={article} />
          )}
        </main>
      </div>
    </div>
  );
}

export default Root;
