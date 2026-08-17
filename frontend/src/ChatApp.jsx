import { useState, useRef, useEffect } from "react";
import "./ChatApp.css";

// ── KOSIS 통계표 주소 ──────────────────────────────────
function kosisTableUrl(orgId, tblId) {
  return `https://kosis.kr/statHtml/statHtml.do?orgId=${orgId}&tblId=${tblId}`;
}

// 검증 진행 단계 — 사용자가 알아듣기 쉬운 표현으로
const STAGES = [
  "문장을 분석하는 중이에요",
  "관련 통계표를 찾는 중이에요",
  "가장 알맞은 표를 고르는 중이에요",
  "통계표와 대조해 확인하는 중이에요",
  "수치를 계산하는 중이에요",
];

const VERDICTS = {
  match: { label: "일치", className: "match" },
  mismatch: { label: "불일치", className: "mismatch" },
  notfound: { label: "검증 불가능 · 매칭 실패", className: "unverifiable" },
  outofscope: { label: "검증 불가능 · 대상 밖", className: "unverifiable" },
};

// 데모 판정 시나리오 (검증 대상 문장에 순서대로 배정)
const DEMO_CASES = [
  {
    verdict: "match",
    answer:
      "통계청 '자산별 생산자본스톡(명목, 연말기준)'에 따르면 2024년 고정자산의 생산자본스톡은 " +
      "11,694,600.8십억 원으로, 전년(11,252,865.6십억 원) 대비 3.93% 증가했습니다. 기사 내용과 일치합니다.",
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
      "관련 통계표 후보를 검색했으나 질의의 지표와 매칭되는 표를 찾지 못했습니다. " +
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
    candidates: null,
  },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 문장 분리 / 숫자 감지 (원래 버전과 동일)
function splitSentences(text) {
  return text.split(/(?<=[.!?。\n])(?!\d)/);
}
function hasNumber(sentence) {
  return /\d/.test(sentence);
}
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

// ── 후보 목록 (접기/펼치기) ─────────────────────────────
function Candidates({ candidates }) {
  const [open, setOpen] = useState(false);
  if (!candidates) return null;
  return (
    <div className="c-cand-block">
      <button className="c-cand-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} 고려한 통계표 후보 {candidates.length}개
      </button>
      {open && (
        <div className="c-cand-list">
          {candidates.map((cd) => {
            const [orgId, tblId] = cd.key.split(":");
            const selected = cd.status === "선택";
            return (
              <a
                key={cd.key}
                className={`c-cand-row ${selected ? "selected" : ""}`}
                href={kosisTableUrl(orgId, tblId)}
                target="_blank"
                rel="noreferrer"
              >
                <span className="c-cand-rank">{cd.rank}</span>
                <span className="c-cand-name">{cd.name}</span>
                <span className="c-cand-score">score {cd.score.toFixed(1)}</span>
                <span className={`c-cand-status ${selected ? "ok" : "no"}`}>
                  {cd.status}
                </span>
              </a>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── 문장 판정 상세 ──────────────────────────────────────
function ClaimDetail({ seg }) {
  const meta = VERDICTS[seg.verdict];
  return (
    <span className={`c-detail ${meta.className}`}>
      <span className="c-detail-verdict">{meta.label}</span>
      <span className="c-detail-answer">{seg.answer}</span>
      {seg.calc && <span className="c-calc">{seg.calc}</span>}
      {seg.table && (
        <span className="c-table-block">
          <a
            className="c-table"
            href={kosisTableUrl(seg.table.orgId, seg.table.tblId)}
            target="_blank"
            rel="noreferrer"
          >
            📊 {seg.table.name} 표 열기
          </a>
          {seg.table.path && <span className="c-path">📍 {seg.table.path}</span>}
        </span>
      )}
      <Candidates candidates={seg.candidates} />
    </span>
  );
}

// ── 결과 말풍선: 원래 버전처럼 기사 전체 + 클릭 판정 ─────
function ArticleResult({ segments }) {
  const [openId, setOpenId] = useState(null);

  const counts = { match: 0, mismatch: 0, unverifiable: 0 };
  segments.forEach((s) => {
    if (!s.verifiable) return;
    if (s.verdict === "match") counts.match += 1;
    else if (s.verdict === "mismatch") counts.mismatch += 1;
    else counts.unverifiable += 1;
  });

  return (
    <div className="c-article-card">
      <div className="c-summary">
        <span className="c-summary-item match">
          <strong>{counts.match}</strong> 일치
        </span>
        <span className="c-summary-item mismatch">
          <strong>{counts.mismatch}</strong> 불일치
        </span>
        <span className="c-summary-item unverifiable">
          <strong>{counts.unverifiable}</strong> 검증 불가능
        </span>
        <span className="c-summary-hint">밑줄 친 문장을 클릭하세요</span>
      </div>

      <div className="c-article-text">
        {segments.map((seg) => {
          if (!seg.verifiable) return <span key={seg.id}>{seg.text}</span>;
          const meta = VERDICTS[seg.verdict];
          const open = openId === seg.id;
          return (
            <span key={seg.id} className={`c-claim-wrap ${open ? "open" : ""}`}>
              <span
                className={`c-claim ${meta.className} ${open ? "active" : ""}`}
                onClick={() => setOpenId(open ? null : seg.id)}
                role="button"
                tabIndex={0}
              >
                {seg.text}
              </span>
              {open && <ClaimDetail seg={seg} />}
            </span>
          );
        })}
      </div>
    </div>
  );
}

// 사용자가 넣은 기사 원문 말풍선 — 길면 접어두고 클릭 시 펼침
function UserArticleBubble({ text }) {
  const [open, setOpen] = useState(false);
  const isLong = text.length > 100; // 대략 3줄 이상이면 접기 제공
  return (
    <div className="c-bubble user">
      <div className={`c-user-text ${!open && isLong ? "clamp" : ""}`}>
        {text}
      </div>
      {isLong && (
        <button className="c-user-toggle" onClick={() => setOpen((o) => !o)}>
          {open ? "접기 ▴" : "원문 펼치기 ▾"}
        </button>
      )}
    </div>
  );
}

// 현재 진행률 목표치(%) 계산: 완료 문장 + 현재 문장의 단계 진행분
function calcTargetPercent(logs, totalSent) {
  let unitsDone = 0;
  logs.forEach((l) => {
    if (l.status === "done") unitsDone += 1;
    else if (l.status === "running")
      unitsDone += (l.stageIdx || 0) / STAGES.length;
  });
  return Math.min(99, Math.round((unitsDone / (totalSent || 1)) * 100));
}

// ── 진행(시간) 말풍선 — 주장별 시간 + 전체 시간 ─────────
// 완료 후에도 대화에 그대로 남습니다.
function ProgressBubble({ logs, nowTs, startTs, totalMs, done, pct }) {
  const totalSec = done ? totalMs / 1000 : Math.max(0, nowTs - startTs) / 1000;
  const percent = done ? 100 : pct ?? 0; // pct는 부드럽게 올라가는 애니메이션 값

  return (
    <div className="c-progress">
      <div className="c-progress-top">
        {/* 동그라미 버퍼링 (퍼센트 링) */}
        <div
          className={`c-ring ${done ? "done" : "loading"}`}
          style={{ "--pct": percent }}
        >
          <span className="c-ring-num">{done ? "✓" : `${percent}%`}</span>
        </div>
        <div className="c-progress-meta">
          <strong>{done ? "검증 완료" : "검증 중…"}</strong>
          <span className="c-elapsed">전체 {totalSec.toFixed(1)}s</span>
        </div>
      </div>

      <div className="c-sent-log">
        {logs.map((l) => {
          const status = done ? "done" : l.status;
          if (status === "done") {
            return (
              <div key={l.n} className="c-sent-line done">
                <span className="c-sent-icon">✓</span>
                <span className="c-sent-label">
                  문장 {l.n}/{l.total} · {l.verdict}
                </span>
                <span className="c-sent-time">
                  {(l.durMs / 1000).toFixed(1)}s
                </span>
              </div>
            );
          }
          if (status === "running") {
            const sec = Math.max(0, nowTs - l.startTs) / 1000;
            return (
              <div key={l.n} className="c-sent-line running">
                <span className="c-sent-icon">▸</span>
                <span className="c-sent-label">
                  문장 {l.n}/{l.total} · {l.stage}
                </span>
                <span className="c-sent-time">{sec.toFixed(1)}s</span>
              </div>
            );
          }
          // 대기 중 (미리 표시되어 자리를 잡아둠)
          return (
            <div key={l.n} className="c-sent-line pending">
              <span className="c-sent-icon">·</span>
              <span className="c-sent-label">
                문장 {l.n}/{l.total}
              </span>
              <span className="c-sent-time">대기 중</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ChatApp({ initialArticle }) {
  const [messages, setMessages] = useState(
    initialArticle
      ? []
      : [
          {
            role: "assistant",
            kind: "text",
            text:
              "안녕하세요! 검증할 기사를 붙여넣어 주세요. 수치 문장을 KOSIS와 대조해, " +
              "기사 전체에 밑줄로 표시하고 문장을 누르면 판정 근거를 보여드립니다.",
          },
        ]
  );
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sentLogs, setSentLogs] = useState([]); // 주장별 진행: {n, total, startTs, verdict, durMs}
  const [nowTs, setNowTs] = useState(0); // 실시간 시계
  const [pct, setPct] = useState(0); // 화면에 표시되는 진행률(부드럽게 1씩 증가)
  const startAllRef = useRef(0);
  const bottomRef = useRef(null);
  const timerRef = useRef(null);
  const animRef = useRef(null); // 퍼센트 카운트업 타이머
  const targetPctRef = useRef(0); // 목표 진행률
  const pctRef = useRef(0); // 현재 표시 진행률(동기 읽기용)
  const startedRef = useRef(false);

  // 새 메시지/로딩 시작 때만 아래로 스크롤 (진행 중 문장 갱신에는 스크롤 안 함)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // 첫 화면(랜딩)에서 넘어온 기사를 자동으로 검증
  useEffect(() => {
    if (initialArticle && !startedRef.current) {
      startedRef.current = true;
      runVerify(initialArticle);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialArticle]);

  function handleSend() {
    if (!input.trim() || loading) return;
    const text = input;
    setInput("");
    runVerify(text);
  }

  async function runVerify(rawText) {
    const text = (rawText || "").trim();
    if (!text || loading) return;
    setMessages((prev) => [...prev, { role: "user", kind: "text", text }]);
    setLoading(true);
    setSentLogs([]);

    const analyzed = mockAnalyze(text);
    const verifiable = analyzed.filter((s) => s.verifiable);

    // 맨 처음에 검증 대상 문장 개수를 안내
    setMessages((prev) => [
      ...prev,
      {
        role: "assistant",
        kind: "text",
        text:
          verifiable.length > 0
            ? `기사에서 검증이 필요한 수치 문장 ${verifiable.length}개를 찾았어요. 하나씩 확인해 볼게요.`
            : "기사에서 검증이 필요한 수치 문장을 찾지 못했어요.",
      },
    ]);

    // 검증 대상이 없으면 결과(원문)만 바로 보여주고 종료
    if (verifiable.length === 0) {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", kind: "article", segments: analyzed },
      ]);
      setLoading(false);
      return;
    }

    const t0 = performance.now();
    startAllRef.current = t0;
    setNowTs(t0);
    timerRef.current = setInterval(() => setNowTs(performance.now()), 100);

    // 진행률을 목표치까지 1씩 순서대로 올려 표시 (뚝뚝 튀지 않게)
    setPct(0);
    pctRef.current = 0;
    targetPctRef.current = 0;
    animRef.current = setInterval(() => {
      setPct((p) => {
        const next = p < targetPctRef.current ? p + 1 : p;
        pctRef.current = next;
        return next;
      });
    }, 45);

    // sentLogs 갱신 + 진행률 목표치 갱신을 함께
    const sync = () => {
      setSentLogs([...run]);
      targetPctRef.current = calcTargetPercent(run, verifiable.length);
    };

    // 주장(문장)마다 검증 시간을 측정하며 진행 표시
    // 모든 문장 행을 먼저 만들어 화면에 고정 (대기 상태)
    const run = verifiable.map((_, i) => ({
      n: i + 1,
      total: verifiable.length,
      status: "pending", // pending → running → done
      startTs: null,
      stage: null,
      stageIdx: 0,
      verdict: null,
      durMs: null,
    }));
    sync();

    for (let i = 0; i < verifiable.length; i++) {
      const startTs = performance.now();
      run[i] = { ...run[i], status: "running", startTs, stage: STAGES[0] };
      sync();
      // 단계는 누적하지 않고 한 줄에서 그때그때 교체
      for (let s = 0; s < STAGES.length; s++) {
        run[i] = { ...run[i], stage: STAGES[s], stageIdx: s + 1 };
        sync();
        await sleep(350 + Math.random() * 350);
      }
      const durMs = performance.now() - startTs;
      const label = VERDICTS[verifiable[i].verdict].label;
      run[i] = { ...run[i], status: "done", verdict: label, durMs, stage: null };
      sync();
    }

    // 마무리: 타이머 정리하고 100%로 (중간 카운트업은 anim 인터벌이 담당)
    clearInterval(animRef.current);
    clearInterval(timerRef.current);
    setPct(100);
    pctRef.current = 100;
    const totalMs = performance.now() - t0;

    // 1) 진행(시간) 말풍선을 대화에 그대로 남기고
    // 2) 결과는 별도 말풍선으로 추가
    setMessages((prev) => [
      ...prev,
      { role: "assistant", kind: "progress", logs: run, totalMs },
      { role: "assistant", kind: "article", segments: analyzed },
    ]);
    setLoading(false);
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <div className="chat-app">
      <div className="chat-body">
        {messages.map((msg, i) => {
          if (msg.kind === "article") {
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant result-bubble">
                  <ArticleResult segments={msg.segments} />
                </div>
              </div>
            );
          }
          if (msg.kind === "progress") {
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant progress-bubble">
                  <ProgressBubble logs={msg.logs} totalMs={msg.totalMs} done />
                </div>
              </div>
            );
          }
          if (msg.role === "user") {
            return (
              <div key={i} className="c-row user">
                <UserArticleBubble text={msg.text} />
              </div>
            );
          }
          return (
            <div key={i} className="c-row assistant">
              <div className="c-bubble assistant">{msg.text}</div>
            </div>
          );
        })}

        {/* 진행 중 실시간 말풍선 */}
        {loading && (
          <div className="c-row assistant">
            <div className="c-bubble assistant progress-bubble">
              <ProgressBubble
                logs={sentLogs}
                nowTs={nowTs}
                startTs={startAllRef.current}
                pct={pct}
                done={false}
              />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input">
        <textarea
          className="c-textarea"
          placeholder="기사를 붙여넣고 Enter (줄바꿈은 Shift+Enter)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
        />
        <button
          className="c-send"
          onClick={handleSend}
          disabled={loading || !input.trim()}
        >
          전송
        </button>
      </div>
    </div>
  );
}

export default ChatApp;
