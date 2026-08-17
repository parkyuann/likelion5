import { useState, useRef } from "react";
import "./App.css";

// ── KOSIS 통계표 주소 만들기 ────────────────────────────
function kosisTableUrl(orgId, tblId) {
  return `https://kosis.kr/statHtml/statHtml.do?orgId=${orgId}&tblId=${tblId}`;
}

// 실제 백엔드는 문장별로 파싱 → 후보검색 → 재점수 → RAG → 계산을 거칩니다.
// (UI에는 세부 단계 대신 "문장 n/N 검증 중 + 소요 시간"만 간결히 표시)

// ── 판정 종류별 표시 정보 ───────────────────────────────
const VERDICTS = {
  match: { label: "일치", className: "match" },
  mismatch: { label: "불일치", className: "mismatch" },
  notfound: { label: "검증 불가능 · 매칭 실패", className: "unverifiable" },
  outofscope: { label: "검증 불가능 · 대상 밖", className: "unverifiable" },
};

// ── 데모용 판정 시나리오 (검증 대상 문장에 순서대로 배정) ──
// 실제로는 백엔드가 문장별로 아래 형태의 결과를 돌려줍니다.
const DEMO_CASES = [
  {
    verdict: "match",
    answer:
      "통계청이 발표한 '자산별 생산자본스톡(명목, 연말기준)' 통계에 따르면, " +
      "2024년 고정자산의 생산자본스톡은 명목으로 11,694,600.8십억 원입니다. " +
      "이는 전년(11,252,865.6십억 원) 대비 3.93% 증가한 수치로, 기사 내용과 일치합니다.",
    calc: "(11,694,600.8 − 11,252,865.6) ÷ 11,252,865.6 × 100 = +3.93%  (증감액 441,735.2십억 원)",
    table: {
      name: "자산별 생산자본스톡(명목, 연말기준)",
      orgId: "101",
      tblId: "DT_104Y260",
      path: "통계청 › 자산별 생산자본스톡(명목, 연말기준) › 생산자본스톡(명목) › 고정자산 › 2024년",
    },
    candidates: [
      { rank: 1, key: "101:DT_104Y260", name: "자산별 생산자본스톡(명목, 연말기준)", score: 6.0, status: "선택" },
      { rank: 2, key: "301:DT_200Y134", name: "주체별 총고정자본형성(계절조정, 실질, 분기)", score: 2.0, status: "지표없음" },
      { rank: 3, key: "101:DT_1COA107", name: "소유주체/자산소분류별 유형고정자산의 평균내용연수", score: 2.0, status: "지표없음" },
      { rank: 4, key: "448:DT_448001_A011", name: "전년 대비 임금 동향", score: 2.0, status: "지표없음" },
      { rank: 5, key: "370:TX_37002_A096", name: "자산자본의 회전율", score: 2.0, status: "지표없음" },
    ],
  },
  {
    verdict: "mismatch",
    answer:
      "'경제활동인구조사'의 실업률 표에 따르면 해당 월 실업률은 2.7%입니다. " +
      "기사에 제시된 5.2%와 2.5%p 차이가 있어 불일치로 판정합니다.",
    calc: "기사 5.2%  vs  KOSIS 2.7%  →  차이 2.5%p",
    table: {
      name: "경제활동인구조사: 실업률",
      orgId: "101",
      tblId: "DT_1DA7001S",
      path: "통계청 › 경제활동인구조사 › 실업률 › 월별",
    },
    candidates: [
      { rank: 1, key: "101:DT_1DA7001S", name: "성/연령별 실업률", score: 5.5, status: "선택" },
      { rank: 2, key: "101:DT_1DA7002S", name: "교육정도별 실업률", score: 2.0, status: "지표없음" },
      { rank: 3, key: "101:DT_1DA7104S", name: "산업별 취업자", score: 2.0, status: "지표없음" },
      { rank: 4, key: "101:DT_1DA7218S", name: "실업자 구직기간", score: 2.0, status: "지표없음" },
      { rank: 5, key: "118:DT_118N_A001", name: "고용보험 가입 현황", score: 2.0, status: "지표없음" },
    ],
  },
  {
    verdict: "notfound",
    answer:
      "관련 통계표 후보를 검색했으나, 질의의 지표와 매칭되는 표를 찾지 못했습니다. " +
      "(모든 후보 feasible=False · 추후 개선 대상)",
    calc: null,
    table: null,
    candidates: [
      { rank: 1, key: "101:DT_1B040A3", name: "시도별 가구 추계", score: 2.0, status: "지표없음" },
      { rank: 2, key: "101:DT_1JC1501", name: "동물등록 현황", score: 2.0, status: "지표없음" },
      { rank: 3, key: "154:DT_154N_012", name: "농림어업 조사", score: 2.0, status: "지표없음" },
      { rank: 4, key: "101:DT_1IN1502", name: "인구주택총조사", score: 2.0, status: "지표없음" },
      { rank: 5, key: "115:DT_115N_A01", name: "반려동물 연관산업", score: 2.0, status: "지표없음" },
    ],
  },
  {
    verdict: "outofscope",
    answer:
      "미래 전망(예측)에 해당하여 국가통계로 대조할 수 있는 주장이 아닙니다. " +
      "사전 분류 단계(claim_class=전망예측)에서 검증 대상에서 제외했습니다.",
    calc: null,
    table: null,
    candidates: null, // 사전 필터에서 제외 → 후보 검색 자체를 안 함
  },
];

// ── 문장 분리 / 숫자 감지 ───────────────────────────────
function splitSentences(text) {
  return text.split(/(?<=[.!?。\n])(?!\d)/);
}
function hasNumber(sentence) {
  return /\d/.test(sentence);
}

// 숫자가 든 문장에 DEMO_CASES 를 순서대로 배정
function mockAnalyze(article) {
  const segments = splitSentences(article);
  let caseIdx = 0;
  return segments.map((text, i) => {
    if (text.trim() && hasNumber(text)) {
      const demo = DEMO_CASES[caseIdx % DEMO_CASES.length];
      caseIdx += 1;
      return { id: i, text, verifiable: true, ...demo };
    }
    return { id: i, text, verifiable: false };
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function App() {
  const [article, setArticle] = useState("");
  const [segments, setSegments] = useState(null);
  const [openId, setOpenId] = useState(null);
  const [showCand, setShowCand] = useState(false); // 후보 5개 펼침 여부
  const [loading, setLoading] = useState(false);
  const [sentLogs, setSentLogs] = useState([]); // 문장별 진행: {n, total, startTs, verdict, durMs}
  const [totalCount, setTotalCount] = useState(0); // 검증 대상 문장 수
  const [nowTs, setNowTs] = useState(0); // 실시간 시계(경과시간 계산용)
  const startAllRef = useRef(0);
  const timerRef = useRef(null);

  async function handleVerify() {
    if (!article.trim() || loading) return;
    setLoading(true);
    setSegments(null);
    setOpenId(null);
    setSentLogs([]);

    const analyzed = mockAnalyze(article);
    const verifiable = analyzed.filter((s) => s.verifiable);
    setTotalCount(verifiable.length);

    // 실시간 경과시간 표시용 타이머 (100ms마다 화면 갱신)
    const t0 = performance.now();
    startAllRef.current = t0;
    setNowTs(t0);
    timerRef.current = setInterval(() => setNowTs(performance.now()), 100);

    // 문장마다 "검증 중" 한 줄을 추가하고, 끝나면 판정+소요시간으로 갱신
    for (let i = 0; i < verifiable.length; i++) {
      const startTs = performance.now();
      setSentLogs((prev) => [
        ...prev,
        { n: i + 1, total: verifiable.length, startTs, verdict: null, durMs: null },
      ]);
      // 실제 백엔드 호출 자리 (지금은 가짜 지연). 문장마다 소요시간이 다르도록.
      await sleep(900 + Math.random() * 1400);
      const durMs = performance.now() - startTs;
      const label = VERDICTS[verifiable[i].verdict].label;
      setSentLogs((prev) =>
        prev.map((x) => (x.n === i + 1 ? { ...x, verdict: label, durMs } : x))
      );
    }

    clearInterval(timerRef.current);
    setNowTs(performance.now());
    await sleep(500);

    setSegments(analyzed);
    setLoading(false);
  }

  function toggleSentence(id) {
    setOpenId((cur) => (cur === id ? null : id));
    setShowCand(false); // 다른 문장 열면 후보 목록은 접힌 상태로
  }

  // 결과 화면이 아닐 때(입력·검증중)는 세로 중앙 정렬
  const centered = !segments;

  return (
    <div className={`page ${centered ? "center" : ""}`}>
      <header className="header">
        <span className="brand-badge">국가통계 기반 팩트체크</span>
        <h1>
          <span className="logo-mark">✓</span> KOSIS 팩트체크
        </h1>
        <p className="subtitle">
          뉴스 속 수치 주장을 KOSIS 국가통계와 대조해 검증합니다.
          {centered ? "" : " 밑줄 친 문장을 클릭해 근거를 확인하세요."}
        </p>
      </header>

      {/* 입력 영역 */}
      {!segments && !loading && (
        <section className="input-card">
          <textarea
            className="textarea"
            placeholder="여기에 기사 전문을 붙여넣으세요…"
            value={article}
            onChange={(e) => setArticle(e.target.value)}
            rows={10}
          />
          <div className="actions">
            <span className="char-count">{article.length}자</span>
            <button
              className="verify-btn"
              onClick={handleVerify}
              disabled={!article.trim()}
            >
              검증하기
            </button>
          </div>
        </section>
      )}

      {/* 실시간 진행 (문장별 + 전체 소요시간) */}
      {loading && (
        <div className="progress-card">
          <div className="progress-header">
            <div className="progress-title">
              <div className="spinner" />
              <strong>검증 중…</strong>
              <span className="progress-count">
                수치 문장 {totalCount}개
              </span>
            </div>
            <span className="progress-total">
              전체 {((nowTs - startAllRef.current) / 1000).toFixed(1)}s
            </span>
          </div>
          <div className="sent-log">
            {sentLogs.map((s) => {
              const running = s.verdict === null;
              const sec = running
                ? Math.max(0, nowTs - s.startTs) / 1000
                : s.durMs / 1000;
              return (
                <div
                  key={s.n}
                  className={`sent-line ${running ? "running" : "done"}`}
                >
                  <span className="sent-icon">{running ? "▸" : "✓"}</span>
                  <span className="sent-label">
                    문장 {s.n}/{s.total}{" "}
                    {running ? "검증 중…" : `· ${s.verdict}`}
                  </span>
                  <span className="sent-time">{sec.toFixed(1)}s</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 결과: 원문 + 클릭 가능한 검증 문장 */}
      {segments && (
        <section className="article-view">
          {/* 판정 요약 (건수) */}
          {(() => {
            const c = { match: 0, mismatch: 0, unverifiable: 0 };
            segments.forEach((s) => {
              if (!s.verifiable) return;
              if (s.verdict === "match") c.match += 1;
              else if (s.verdict === "mismatch") c.mismatch += 1;
              else c.unverifiable += 1;
            });
            return (
              <div className="summary">
                <div className="summary-item match">
                  <strong>{c.match}</strong>
                  <span>일치</span>
                </div>
                <div className="summary-item mismatch">
                  <strong>{c.mismatch}</strong>
                  <span>불일치</span>
                </div>
                <div className="summary-item unverifiable">
                  <strong>{c.unverifiable}</strong>
                  <span>검증 불가능</span>
                </div>
              </div>
            );
          })()}

          <div className="legend">
            <span className="legend-item match">일치</span>
            <span className="legend-item mismatch">불일치</span>
            <span className="legend-item unverifiable">검증 불가능</span>
            <span className="legend-hint">밑줄 친 문장을 클릭하세요</span>
          </div>

          <article className="article-text">
            {segments.map((seg) => {
              if (!seg.verifiable) {
                return <span key={seg.id}>{seg.text}</span>;
              }
              const meta = VERDICTS[seg.verdict];
              const open = openId === seg.id;
              return (
                <span
                  key={seg.id}
                  className={`claim-wrap ${open ? "open" : ""}`}
                >
                  <span
                    className={`claim ${meta.className} ${open ? "active" : ""}`}
                    onClick={() => toggleSentence(seg.id)}
                    role="button"
                    tabIndex={0}
                  >
                    {seg.text}
                  </span>
                  {open && (
                    <span className={`detail ${meta.className}`}>
                      <span className="detail-head">
                        <span className="detail-verdict">{meta.label}</span>
                      </span>

                      {/* RAG Reasoning 최종 답변 */}
                      <span className="detail-answer">{seg.answer}</span>

                      {/* 표연산(계산식) */}
                      {seg.calc && <span className="detail-calc">{seg.calc}</span>}

                      {/* 근거 표 + 경로 */}
                      {seg.table && (
                        <span className="detail-table-block">
                          <a
                            className="detail-table"
                            href={kosisTableUrl(seg.table.orgId, seg.table.tblId)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            📊 {seg.table.name} 표 열기
                          </a>
                          {seg.table.path && (
                            <span className="detail-path">
                              📍 {seg.table.path}
                            </span>
                          )}
                        </span>
                      )}

                      {/* 고려한 후보 5개 (접기/펼치기) */}
                      {seg.candidates && (
                        <span className="cand-block">
                          <button
                            className="cand-toggle"
                            onClick={() => setShowCand((v) => !v)}
                          >
                            {showCand ? "▾" : "▸"} 고려한 통계표 후보{" "}
                            {seg.candidates.length}개
                          </button>
                          {showCand && (
                            <span className="cand-list">
                              {seg.candidates.map((cd) => {
                                const [orgId, tblId] = cd.key.split(":");
                                const selected = cd.status === "선택";
                                return (
                                  <a
                                    key={cd.key}
                                    className={`cand-row ${selected ? "selected" : ""}`}
                                    href={kosisTableUrl(orgId, tblId)}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    <span className="cand-rank">{cd.rank}</span>
                                    <span className="cand-name">{cd.name}</span>
                                    <span className="cand-score">
                                      score {cd.score.toFixed(1)}
                                    </span>
                                    <span
                                      className={`cand-status ${selected ? "ok" : "no"}`}
                                    >
                                      {cd.status}
                                    </span>
                                  </a>
                                );
                              })}
                            </span>
                          )}
                        </span>
                      )}
                    </span>
                  )}
                </span>
              );
            })}
          </article>

          <button
            className="reset-btn"
            onClick={() => {
              setSegments(null);
              setOpenId(null);
            }}
          >
            다른 기사 검증하기
          </button>
        </section>
      )}
    </div>
  );
}

export default App;
