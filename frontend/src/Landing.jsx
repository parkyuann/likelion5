import { useState } from "react";
import "./Landing.css";

// 첫 화면: 기사를 입력받아 검증을 시작합니다. 제출하면 챗봇으로 전환됩니다.
function Landing({ onSubmit }) {
  const [text, setText] = useState("");

  function submit() {
    if (!text.trim()) return;
    onSubmit(text.trim());
  }

  function handleKeyDown(e) {
    // Ctrl/⌘+Enter 로도 제출
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="landing">
      <header className="landing-header">
        <span className="brand-badge">국가통계 기반 팩트체크</span>
        <h1>
          <span className="logo-mark">✓</span> KOSIS 팩트체크
        </h1>
        <p className="landing-sub">
          뉴스 기사를 붙여넣으면 수치 주장을 KOSIS 국가통계와 대조해 검증합니다.
        </p>
      </header>

      <div className="landing-card">
        <textarea
          className="landing-textarea"
          placeholder="여기에 기사 전문을 붙여넣으세요…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={9}
        />
        <div className="landing-actions">
          <span className="landing-count">{text.length}자</span>
          <button
            className="landing-btn"
            onClick={submit}
            disabled={!text.trim()}
          >
            검증하기
          </button>
        </div>
      </div>
    </div>
  );
}

export default Landing;
