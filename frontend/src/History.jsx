import { useState } from "react";
import { loadHistory, clearHistory } from "./history";
import { ArticleResult } from "./ChatApp.jsx";
import "./History.css";

function summarize(segments) {
  const c = { match: 0, mismatch: 0, unverifiable: 0 };
  (segments || []).forEach((s) => {
    if (!s.verifiable) return;
    if (s.verdict === "match") c.match += 1;
    else if (s.verdict === "mismatch") c.mismatch += 1;
    else c.unverifiable += 1;
  });
  return c;
}

function titleOf(article) {
  const line = (article || "").trim().replace(/\s+/g, " ");
  return line.length > 46 ? line.slice(0, 46) + "…" : line || "(내용 없음)";
}

function formatTs(ts) {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}.${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(
    d.getHours()
  )}:${p(d.getMinutes())}`;
}

function History() {
  const [records, setRecords] = useState(() => loadHistory());
  const [selected, setSelected] = useState(null);

  function handleClear() {
    if (!window.confirm("검증 기록을 모두 삭제할까요?")) return;
    clearHistory();
    setRecords([]);
    setSelected(null);
  }

  // ── 상세 보기 ──
  if (selected) {
    return (
      <div className="history">
        <div className="history-bar">
          <button className="history-back" onClick={() => setSelected(null)}>
            ← 목록으로
          </button>
          <span className="history-detail-date">{formatTs(selected.ts)}</span>
        </div>
        <div className="history-detail">
          <ArticleResult segments={selected.segments} />
        </div>
      </div>
    );
  }

  // ── 목록 ──
  return (
    <div className="history">
      <div className="history-bar">
        <span className="history-title">검증 기록</span>
        {records.length > 0 && (
          <button className="history-clear" onClick={handleClear}>
            전체 삭제
          </button>
        )}
      </div>

      {records.length === 0 ? (
        <div className="history-empty">
          아직 검증한 기록이 없어요.
          <br />
          기사를 검증하면 여기에 쌓입니다.
        </div>
      ) : (
        <ul className="history-list">
          {records.map((r) => {
            const c = summarize(r.segments);
            return (
              <li key={r.id}>
                <button
                  className="history-item"
                  onClick={() => setSelected(r)}
                >
                  <div className="history-item-top">
                    <span className="history-item-title">
                      {titleOf(r.article)}
                    </span>
                    <span className="history-item-date">{formatTs(r.ts)}</span>
                  </div>
                  <div className="history-item-counts">
                    <span className="hc match">{c.match} 일치</span>
                    <span className="hc mismatch">{c.mismatch} 불일치</span>
                    <span className="hc unverifiable">
                      {c.unverifiable} 검증 불가능
                    </span>
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export default History;
