import { useState, useRef, useEffect } from "react";
import "./ChatApp.css";
import { analyzeInput, analyzeImage, verifyArticleDevelop, ApiError } from "./api.js";
import { ImageIcon, AlertIcon, DocIcon, LinkIcon, CheckIcon, RefreshIcon, QuestionIcon } from "./icons.jsx";
import { mockToDisplayMessages } from "./mockVerificationData.js";
import { mockVerifyArticle } from "./uiMockData.js";

// ── KOSIS 통계표 주소 ──────────────────────────────────
function kosisTableUrl(orgId, tblId) {
  return `https://kosis.kr/statHtml/statHtml.do?orgId=${orgId}&tblId=${tblId}`;
}

const VERDICTS = {
  match: { label: "근거 확인", className: "match" },
  mismatch: { label: "비교 결과 확인", className: "mismatch" },
  notfound: { label: "추가 확인 필요 · 매칭 실패", className: "unverifiable" },
  outofscope: { label: "추가 확인 필요 · 대상 밖", className: "unverifiable" },
};

// 검증 진행 단계(문장별 진행 로그에 표시)
const STAGES = [
  "문장을 분석하는 중이에요",
  "관련 통계표를 찾는 중이에요",
  "가장 알맞은 표를 고르는 중이에요",
  "통계표와 대조해 확인하는 중이에요",
  "수치를 계산하는 중이에요",
];

// ── 고려한 통계표 후보 목록 (접기/펼치기) ──────────────
function Candidates({ candidates }) {
  const [open, setOpen] = useState(false);
  if (!candidates || candidates.length === 0) return null;
  return (
    <div className="c-cand-block">
      <button className="c-cand-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} 고려한 통계표 후보 {candidates.length}개
      </button>
      {open && (
        <div className="c-cand-list">
          {candidates.map((cd) => {
            const [orgId, tblId] = String(cd.key || ":").split(":");
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
                {typeof cd.score === "number" && (
                  <span className="c-cand-score">score {cd.score.toFixed(1)}</span>
                )}
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

// ── 문장 판정 상세 (클릭 시 문장 아래 인라인) ──────────
function ClaimDetail({ seg }) {
  const meta = VERDICTS[seg.verdict] || VERDICTS.outofscope;
  const evidence = seg.evidence_answer || seg.evidenceAnswer;
  const answerText = evidence?.text || seg.answer;
  return (
    <span className={`c-detail ${meta.className}`}>
      <span className="c-detail-verdict">{evidence ? "현재 통계 근거" : meta.label}</span>
      {answerText && <span className="c-detail-answer">{answerText}</span>}
      {seg.calc && <span className="c-calc">{seg.calc}</span>}
      {seg.table && (
        <span className="c-table-block">
          <a
            className="c-table"
            href={seg.table.href || kosisTableUrl(seg.table.orgId, seg.table.tblId)}
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

// ── 결과: 기사 전체 + 클릭 시 인라인 판정 ──────────────
function ArticleResult({ segments }) {
  const [openId, setOpenId] = useState(null);

  const counts = { evidence: 0, needsReview: 0 };
  segments.forEach((s) => {
    if (!s.verifiable) return;
    if (s.evidence_answer || s.evidenceAnswer) counts.evidence += 1;
    else counts.needsReview += 1;
  });
  const evidence = segments.find((s) => s.evidence_answer || s.evidenceAnswer);
  const evidenceText = evidence?.evidence_answer?.text || evidence?.evidenceAnswer?.text;

  return (
    <div className="c-article-card">
      {evidenceText && (
        <div className="c-evidence-answer">
          <div className="c-evidence-label">현재 통계 근거</div>
          <p>{evidenceText}</p>
        </div>
      )}
      <div className="c-summary">
        <span className="c-summary-item match">
          <strong>{counts.evidence}</strong> 근거 확인
        </span>
        <span className="c-summary-item mismatch">
          <strong>{counts.needsReview}</strong> 추가 확인 필요
        </span>
        <span className="c-summary-hint">밑줄 친 문장을 클릭하세요</span>
      </div>

      <div className="c-article-text">
        {segments.map((seg) => {
          if (!seg.verifiable) {
            // 수치 주장이 없는 문장도 밑줄은 유지(문장 사이 공백은 제외).
            if ((seg.text || "").trim()) {
              return <span key={seg.id} className="c-sentence">{seg.text}</span>;
            }
            return <span key={seg.id}>{seg.text}</span>;
          }
          const meta = VERDICTS[seg.verdict] || VERDICTS.outofscope;
          const open = openId === seg.id;
          return (
            <span key={seg.id} className={`c-claim-wrap ${open ? "open" : ""}`}>
              <span
                className={`c-claim ${meta.className} ${open ? "active" : ""}`}
                onClick={() => setOpenId(open ? null : seg.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setOpenId(open ? null : seg.id);
                  }
                }}
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

// 사용자 텍스트 말풍선 — 길면 접기
function UserTextBubble({ text }) {
  const [open, setOpen] = useState(false);
  const isLong = text.length > 100;
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

function SourceInputBubble({ text, sourceType, focusQuestion }) {
  const isUrl = sourceType === "url";
  return (
    <div className="c-bubble user c-source-bubble">
      <span className="c-source-kind">
        {isUrl ? <LinkIcon /> : <DocIcon />}
        {isUrl ? "기사 URL" : "기사 본문"}
      </span>
      <strong>{isUrl ? text : `${text.slice(0, 90)}${text.length > 90 ? "…" : ""}`}</strong>
      {focusQuestion && (
        <span className="c-source-focus">
          <small>확인할 내용</small>{focusQuestion}
        </span>
      )}
    </div>
  );
}

function ArticleDocumentResult({ document, extraction, focusQuestion }) {
  const count = extraction?.character_count ?? document.text.length;
  const sourceType = document.source_type || "text";
  const extractionLabel = sourceType === "image" ? "OCR 텍스트 추출" : "본문 추출";
  const extractionCompleted = extraction?.status === "success" || document.text.length > 0;
  return (
    <div className="c-document-result">
      <div className="c-document-status">
        <strong>자료 해석 완료</strong>
        <span>{count.toLocaleString()}자</span>
      </div>
      <div className="c-process-track" aria-label="검증 진행 단계">
        <span className="done"><CheckIcon /> 입력 확인</span><i />
        <span className={extractionCompleted ? "done" : "current"}>
          {extractionCompleted ? <CheckIcon /> : "2"} {extractionLabel}
        </span><i />
        <span className={extractionCompleted ? "current" : "next"}>3</span><b>주장 선택</b><i />
        <span className="next">4</span><b>KOSIS 대조</b>
      </div>
      {focusQuestion && (
        <div className="c-document-focus">
          <small>우선 확인할 질문</small>
          <strong>{focusQuestion}</strong>
        </div>
      )}
      <p className="c-document-note">
        전체 본문에서 질문과 관련된 수치 주장을 고른 뒤 KOSIS 공식 통계와
        대조합니다. 질문이 없으면 검증 가능한 수치를 모두 살펴봅니다.
      </p>
      <div className="c-document-text">{document.text}</div>
    </div>
  );
}

// 저장된 대화 메시지(payload_json 기반)를 화면 메시지 형태로 복원
function historyToMessage(m) {
  if (m.role === "user") {
    // 이미지 원본은 저장되지 않으므로 텍스트 말풍선으로 표시
    if (m.kind === "url" || m.kind === "article") {
      return {
        role: "user",
        kind: "source",
        text: m.content,
        sourceType: m.kind,
        focusQuestion: m.payload?.focus_question || "",
      };
    }
    return { role: "user", kind: "text", text: m.content };
  }
  const p = m.payload || {};
  if (m.kind === "article" || Array.isArray(p.results)) {
    return {
      role: "assistant",
      kind: "article",
      segments: (p.results || []).map((s, i) => ({ id: i, ...s })),
    };
  }
  if (m.kind === "article_document" || p.article_document) {
    return {
      role: "assistant",
      kind: "document",
      document: p.article_document,
      extraction: p.extraction,
      focusQuestion: p.focus_question || "",
    };
  }
  if (m.kind === "error") {
    // 범위 밖 입력은 라이브와 동일하게 부드러운 안내로 복원(빨간 오류 X)
    const kind = p.error_code === "OUT_OF_SCOPE" ? "notice" : "error";
    return { role: "assistant", kind, text: m.content };
  }
  return { role: "assistant", kind: "text", text: p.answer || m.content };
}

const GREETING = {
  role: "assistant",
  kind: "text",
  text: "안녕하세요! 통계 질문을 입력하거나 기사 URL·본문·이미지를 넣어 주세요.",
};

// 범위 밖·검증 불가 안내 뒤에 바로 이어서 시도할 수 있는 통계 질의 예시.
const RECOVERY_EXAMPLES = [
  "2024년 청년 실업률은 얼마야?",
  "지난해 1인 가구 비중은?",
  "2023년 대비 2024년 출생아 수 변화는?",
];

// 막다른 안내에서 사용자가 바로 다음 질문으로 넘어가도록 돕는 예시 칩.
function RecoveryChips({ examples, onPick, disabled }) {
  return (
    <div className="c-recovery">
      <span className="c-recovery-label">이렇게 물어보세요</span>
      <div className="c-recovery-chips">
        {examples.map((example) => (
          <button
            key={example}
            type="button"
            className="c-recovery-chip"
            onClick={() => onPick(example)}
            disabled={disabled}
          >
            <QuestionIcon size="1em" />
            <span>{example}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

const SOURCE_LOADING_STEPS = {
  url: ["URL 안전성 확인", "기사 본문 추출", "수치 주장 선택", "KOSIS 통계 대조"],
  article: ["기사와 질문 분리", "수치 주장 추출", "검증 범위 선택", "KOSIS 통계 대조"],
  image: ["이미지 확인", "OCR로 텍스트 추출", "수치 주장 선택", "KOSIS 통계 대조"],
  query: ["질문 의도 해석", "통계 항목 탐색", "시점·단위 확인", "KOSIS 통계 대조"],
  auto: ["입력 해석", "통계 항목 탐색", "수치 확인", "결과 정리"],
};

// 개발용 로딩 시간 설정. 화면에는 노출하지 않습니다.
// 예: http://localhost:5173/?mockDelay=15 (1~60초)
function mockDelayOverrideMs() {
  if (!import.meta.env.DEV || typeof window === "undefined") return null;
  const seconds = Number(new URLSearchParams(window.location.search).get("mockDelay"));
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  return Math.min(60, Math.max(1, seconds)) * 1000;
}

// 오프라인 데모용 고정 목업 사용 여부. 기본은 실제 파이프라인.
// 예: http://localhost:5173/?mock=1
function mockEnabled() {
  if (!import.meta.env.DEV || typeof window === "undefined") return false;
  const value = new URLSearchParams(window.location.search).get("mock");
  return value === "1" || value === "true";
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function isValidArticleDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  if (year < 1 || month < 1 || month > 12 || day < 1) return false;
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

// ── 진행 말풍선: 원형 링(%) + 문장별 진행 로그 ─────────
// 완료 후에도 대화에 그대로 남으며, 완료 시 문장별 내역은 토글로 접는다.
function ProgressBubble({ progress }) {
  const { done = false, pct = 0, elapsedS = 0, logs = [] } = progress || {};
  const [openLog, setOpenLog] = useState(false);
  const percent = done ? 100 : Math.round(pct);
  // 진행 중엔 항상 보이고, 완료 후엔 토글로 열 때만 보인다.
  const showLog = logs.length > 0 && (!done || openLog);
  return (
    <div className="c-progress">
      <div className="c-progress-top">
        <div className={`c-ring ${done ? "done" : "loading"}`} style={{ "--pct": percent }}>
          <span className="c-ring-num">{done ? "✓" : `${percent}%`}</span>
        </div>
        <div className="c-progress-meta">
          <strong>{done ? "검증 완료" : "검증 중…"}</strong>
          <span className="c-elapsed">전체 {elapsedS.toFixed(1)}s</span>
        </div>
        {done && logs.length > 0 && (
          <button
            type="button"
            className="c-progress-toggle"
            onClick={() => setOpenLog((v) => !v)}
          >
            {openLog ? "접기 ▴" : "문장별 내역 ▾"}
          </button>
        )}
      </div>
      {showLog && (
        <div className="c-sent-log">
          {logs.map((l) => {
            const icon = l.status === "done" ? "✓" : l.status === "running" ? "▸" : "·";
            const label =
              l.status === "done"
                ? `문장 ${l.n}/${l.total} · ${l.verdict}`
                : l.status === "running"
                  ? `문장 ${l.n}/${l.total} · ${l.stage}`
                  : `문장 ${l.n}/${l.total}`;
            const time = l.status === "pending" ? "대기 중" : `${(l.sec || 0).toFixed(1)}s`;
            return (
              <div key={l.n} className={`c-sent-line ${l.status}`}>
                <span className="c-sent-icon">{icon}</span>
                <span className="c-sent-label">{label}</span>
                <span className="c-sent-time">{time}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ChatApp({
  initial,
  initialMessages,
  initialDisplayMessages,
  initialConversationId,
  onSaved,
  onScroll,
  onMessagesChange,
}) {
  const [messages, setMessages] = useState(() => {
    if (initial?.mock) return mockToDisplayMessages(initial.mock).slice(0, 1);
    if (initialDisplayMessages && initialDisplayMessages.length)
      return initialDisplayMessages;
    if (initialMessages && initialMessages.length)
      return initialMessages.map(historyToMessage);
    return initial ? [] : [GREETING];
  });
  const [conversationId, setConversationId] = useState(
    initialConversationId || null
  );
  const [input, setInput] = useState("");
  const [sourceQuestion, setSourceQuestion] = useState("");
  const [pendingImage, setPendingImage] = useState(null); // { file, url }
  const [pendingClarification, setPendingClarification] = useState(null);
  const [loading, setLoading] = useState(false);
  const chatBodyRef = useRef(null);
  const fileRef = useRef(null);
  const startedRef = useRef(false);
  const lastRequestRef = useRef(null); // 오류 시 '다시 시도'로 재실행할 마지막 요청
  const [verificationProgress, setVerificationProgress] = useState({
    done: false,
    pct: 0,
    elapsedS: 0,
    logs: [],
  });
  const progressTimersRef = useRef([]); // 진행 애니메이션 타이머 정리용
  const onMessagesChangeRef = useRef(onMessagesChange);

  useEffect(() => {
    onMessagesChangeRef.current = onMessagesChange;
  }, [onMessagesChange]);

  useEffect(() => {
    onMessagesChangeRef.current?.(messages);
  }, [messages]);

  useEffect(() => {
    const body = chatBodyRef.current;
    if (!body) return undefined;
    const frame = requestAnimationFrame(() => {
      body.scrollTo({ top: body.scrollHeight, behavior: "smooth" });
    });
    return () => cancelAnimationFrame(frame);
  }, [messages, loading, pendingImage]);

  // 랜딩에서 넘어온 초기 요청 자동 실행
  useEffect(() => {
    if (initial && !startedRef.current) {
      startedRef.current = true;
      if (initial.mock) {
        // 기본은 실제 파이프라인. 목업은 ?mock=1 일 때만.
        if (mockEnabled()) {
          runMockVerification(initial.mock, {
            focusQuestion: initial.mock.input.focusQuestion,
          });
        } else {
          verifyText(initial.mock.input.raw, {
            inputType: initial.mock.input.sourceType === "url" ? "url" : "article",
            focusQuestion: initial.mock.input.focusQuestion || "",
          });
        }
        return;
      }
      if (initial.image) verifyImage(initial.image, initial.focusQuestion || "");
      else if (initial.text) verifyText(initial.text, {
        inputType: initial.inputType || "auto",
        focusQuestion: initial.focusQuestion || "",
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial]);

  // ── 결과/에러 렌더 헬퍼 ──
  function pushResult(result, fallbackFocusQuestion = "") {
    if (result.type === "simple_query") {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          kind: "text",
          text: result.answer || "통계 결과를 받았습니다.",
        },
      ]);
    } else if (result.type === "article_document" && result.article_document) {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          kind: "document",
          document: result.article_document,
          extraction: result.extraction,
          focusQuestion: result.focus_question || fallbackFocusQuestion,
        },
      ]);
    } else if (Array.isArray(result.results)) {
      const segments = result.results.map((s, i) => ({ id: i, ...s }));
      setMessages((prev) => [
        ...prev,
        { role: "assistant", kind: "article", segments },
      ]);
    } else {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          kind: "text",
          text: "백엔드 응답을 받았지만 표시할 결과가 없습니다.",
        },
      ]);
    }
  }
  function pushError(error) {
    const message =
      error instanceof ApiError
        ? error.message
        : "백엔드 서버에 연결할 수 없습니다. 서버가 실행 중인지 확인해 주세요.";
    // 범위 밖 입력은 '오류'가 아니라 부드러운 안내로 표시(빨간 경고창 X).
    const kind =
      error instanceof ApiError && error.code === "OUT_OF_SCOPE"
        ? "notice"
        : "error";
    setMessages((prev) => [
      ...prev,
      { role: "assistant", kind, text: message },
    ]);
  }

  function requestClarification(article, result) {
    setPendingClarification({
      ...article,
      question: result.question,
      clarificationAnswers: article.clarificationAnswers || [],
    });
    setMessages((prev) => [
      ...prev,
      {
        role: "assistant",
        kind: "clarification_request",
        text: result.question?.prompt || "확인을 위해 통계 조건을 조금 더 알려주세요.",
      },
    ]);
  }

  // ── 텍스트 검증 ──
  function verifyText(rawText, {
    inputType = "auto",
    focusQuestion = "",
  } = {}) {
    const text = (rawText || "").trim();
    if (!text || loading) return;
    const sourceType = inputType === "auto" && /^https?:\/\/\S+$/i.test(text)
      ? "url"
      : inputType;
    setMessages((prev) => [...prev, {
      role: "user",
      kind: sourceType === "article" || sourceType === "url" ? "source" : "text",
      text,
      sourceType,
      focusQuestion,
    }]);
    runText(text, { inputType: sourceType, focusQuestion });
  }
  function handleResult(result, fallbackFocusQuestion = "") {
    if (result.conversation_id) {
      setConversationId(result.conversation_id);
      onSaved?.(result.conversation_id);
    }
    pushResult(result, fallbackFocusQuestion);
  }
  // 오류도 (잡담이 아니면) 대화로 저장되므로 목록을 갱신하고 대화를 이어간다.
  function handleError(err) {
    const cid =
      err instanceof ApiError ? err.detail?.conversation_id : undefined;
    if (cid) {
      setConversationId(cid);
      onSaved?.(cid);
    }
    pushError(err);
  }

  function waitForMock(ms) {
    // React 개발 모드의 StrictMode 정리 과정에서도 대기 Promise가 고립되지 않도록
    // 짧고 제한된 목업 타이머는 자체적으로 완료되게 둡니다.
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function runMockVerification(mock, { focusQuestion = "", totalMs } = {}) {
    const sourceType = mock.input.sourceType;
    const steps = SOURCE_LOADING_STEPS[sourceType] || SOURCE_LOADING_STEPS.auto;
    const duration = Math.max(
      1000,
      Number(totalMs) || mockDelayOverrideMs() || mock.timing?.totalMs || 8000,
    );
    const weights = mock.timing?.stepWeights?.length === steps.length
      ? mock.timing.stepWeights
      : steps.map(() => 1 / steps.length);
    const completionHold = Math.min(300, duration * 0.1);
    const activeDuration = duration - completionHold;

    setVerificationProgress({
      sourceType,
      completed: [],
      active: 0,
      isMock: true,
      totalMs: duration,
    });
    setLoading(true);

    for (let index = 0; index < steps.length; index += 1) {
      await waitForMock(activeDuration * weights[index]);
      setVerificationProgress({
        sourceType,
        completed: Array.from({ length: index + 1 }, (_, stepIndex) => stepIndex),
        active: Math.min(index + 1, steps.length - 1),
        isMock: true,
        totalMs: duration,
      });
    }

    // 100% 완료 상태를 인지할 수 있도록 설정된 총시간 안에서 잠깐 유지합니다.
    await waitForMock(completionHold);

    setMessages((prev) => [
      ...prev,
      ...mockToDisplayMessages({
        ...mock,
        input: {
          ...mock.input,
          focusQuestion: focusQuestion || mock.input.focusQuestion,
        },
      }).slice(1),
    ]);
    setLoading(false);
  }

  function stopProgressTimers() {
    progressTimersRef.current.forEach((t) => clearInterval(t));
    progressTimersRef.current = [];
  }

  // 네트워크 대기 동안: 경과 시간 + 버퍼링 느낌의 완만한 % 상승(문장 로그 없음)
  function startNetworkProgress() {
    const start = performance.now();
    setVerificationProgress({ done: false, pct: 0, elapsedS: 0, logs: [] });
    const elapsed = setInterval(() => {
      setVerificationProgress((p) => ({ ...p, elapsedS: (performance.now() - start) / 1000 }));
    }, 100);
    const climb = setInterval(() => {
      setVerificationProgress((p) => (p.pct < 85 ? { ...p, pct: p.pct + 1 } : p));
    }, 260);
    progressTimersRef.current = [elapsed, climb];
    return start;
  }

  // 응답 도착 후: 문장별로 판정을 하나씩 채워 넣는 리플레이(실제 판정 + 시뮬레이션 시간)
  async function runVerificationReplay(verifiable, startTs) {
    // 버퍼 % 상승만 멈추고 경과 타이머는 유지
    const [elapsed, climb] = progressTimersRef.current;
    if (climb) clearInterval(climb);
    progressTimersRef.current = elapsed ? [elapsed] : [];

    const total = verifiable.length;
    const run = verifiable.map((_, i) => ({
      n: i + 1, total, status: "pending", verdict: null, stage: null, sec: 0,
    }));
    const bump = () =>
      setVerificationProgress((p) => ({ ...p, logs: run.map((r) => ({ ...r })) }));
    bump();

    for (let i = 0; i < total; i += 1) {
      const st = performance.now();
      run[i].status = "running";
      for (let s = 0; s < STAGES.length; s += 1) {
        run[i].stage = STAGES[s];
        run[i].sec = (performance.now() - st) / 1000;
        const units = i + (s + 1) / STAGES.length;
        const target = Math.min(99, Math.round((units / total) * 100));
        setVerificationProgress((p) => ({
          ...p, pct: Math.max(p.pct, target), logs: run.map((r) => ({ ...r })),
        }));
        await sleep(280 + Math.random() * 320);
      }
      run[i].status = "done";
      run[i].verdict = (VERDICTS[verifiable[i].verdict] || VERDICTS.outofscope).label;
      run[i].sec = (performance.now() - st) / 1000;
      run[i].stage = null;
      bump();
    }
    stopProgressTimers();
    const elapsedS = (performance.now() - startTs) / 1000;
    const logs = run.map((r) => ({ ...r }));
    setVerificationProgress((p) => ({ ...p, done: true, pct: 100, elapsedS, logs }));
    return { elapsedS, logs };
  }

  // 기사 검증 결과: 진행 말풍선(리플레이) + 결과 카드를 대화에 남긴다
  async function showArticleResult(verified, startTs) {
    if (verified.conversation_id) {
      setConversationId(verified.conversation_id);
      onSaved?.(verified.conversation_id);
    }
    const segments = (verified.results || []).map((s, i) => ({ id: i, ...s }));
    const verifiable = segments.filter((s) => s.verifiable);
    let info = { elapsedS: (performance.now() - startTs) / 1000, logs: [] };
    if (verifiable.length > 0) info = await runVerificationReplay(verifiable, startTs);
    setMessages((prev) => [
      ...prev,
      {
        role: "assistant",
        kind: "progress",
        progress: { done: true, pct: 100, elapsedS: info.elapsedS, logs: info.logs },
      },
      { role: "assistant", kind: "article", segments },
    ]);
  }

  async function runText(text, {
    inputType = "auto",
    focusQuestion = "",
    title = "",
    date = "",
    dateSource = null,
    clarificationAnswers = [],
    requestConversationId = conversationId,
  } = {}) {
    // UI 확인용 목업은 ?mock=1 일 때만 사용합니다(기본은 실제 파이프라인).
    // 새 UX(진행 링·결과 카드·인라인 상세)를 색상/수식/후보까지 그대로 렌더한다.
    if (mockEnabled()) {
      lastRequestRef.current = {
        kind: "text", text, inputType, focusQuestion, title, date, dateSource, clarificationAnswers, requestConversationId,
      };
      const startTs = startNetworkProgress();
      setLoading(true);
      try {
        await sleep(600); // 짧은 네트워크 대기 연출
        await showArticleResult(mockVerifyArticle(text), startTs);
      } catch (err) {
        handleError(err);
      } finally {
        stopProgressTimers();
        setLoading(false);
      }
      return;
    }
    lastRequestRef.current = {
      kind: "text", text, inputType, focusQuestion, title, date, dateSource, clarificationAnswers, requestConversationId,
    };
    const isUrl = inputType === "url" || /^https?:\/\/\S+$/i.test(text.trim());
    const startTs = startNetworkProgress();
    setLoading(true);
    try {
      if (isUrl) {
        // URL은 먼저 본문을 확보한 뒤 develop 파이프라인으로 검증한다.
        const prepared = await analyzeInput(text, {
          conversationId: requestConversationId,
          inputType: "url",
          focusQuestion,
        });
        const doc = prepared?.article_document;
        if (prepared?.type === "article_document" && doc?.text) {
          const verified = await verifyArticleDevelop(doc.text, {
            conversationId: prepared.conversation_id || requestConversationId,
            title: doc.title || "",
            date: doc.published_date || "",
            dateSource: doc.published_date ? "url_metadata" : null,
            clarificationAnswers,
          });
          if (verified?.type === "needs_user_input") {
            requestClarification({
              text: doc.text,
              title: doc.title || "",
              inputType: "article",
              focusQuestion,
              date: doc.published_date || "",
              dateSource: doc.published_date ? "url_metadata" : null,
              conversationId: prepared.conversation_id || requestConversationId,
              clarificationAnswers: [],
            }, verified);
          } else if (verified?.type === "article") await showArticleResult(verified, startTs);
          else handleResult(verified, focusQuestion);
        } else {
          handleResult(prepared, focusQuestion);
        }
      } else {
        // 기사 본문/텍스트는 develop 파이프라인으로 검증한다. 단 수치 주장이 없어
        // 기사가 아니면(질문·잡담) 기존 라우터로 넘긴다(질문→KOSIS, 잡담→안내).
        const verified = await verifyArticleDevelop(text, {
          conversationId: requestConversationId,
          title,
          date,
          dateSource,
          clarificationAnswers,
        });
        if (verified?.type === "needs_user_input") {
          requestClarification({
            text,
            title,
            inputType,
            focusQuestion,
            conversationId: requestConversationId,
            clarificationAnswers: [],
          }, verified);
        } else if (verified?.type === "not_article") {
          const routed = await analyzeInput(text, {
            conversationId: requestConversationId,
            inputType,
            focusQuestion,
          });
          handleResult(routed, focusQuestion);
        } else if (verified?.type === "article") {
          await showArticleResult(verified, startTs);
        } else {
          handleResult(verified, focusQuestion);
        }
      }
    } catch (err) {
      handleError(err);
    } finally {
      stopProgressTimers();
      setLoading(false);
    }
  }

  // ── 이미지 검증 ──
  async function inspectImageFile(file) {
    try {
      if (typeof globalThis.createImageBitmap === "function") {
        const bitmap = await globalThis.createImageBitmap(file);
        const valid = bitmap.width > 0 && bitmap.height > 0;
        bitmap.close();
        if (!valid) throw new Error("invalid dimensions");
        return;
      }
      await file.arrayBuffer();
    } catch {
      throw new ApiError("이미지 파일을 읽을 수 없습니다. 다른 이미지를 선택해 주세요.", {
        code: "INVALID_IMAGE",
      });
    }
  }

  function verifyImage(file, focusQuestion = "") {
    if (!file || loading) return;
    const url = URL.createObjectURL(file);
    setMessages((prev) => [
      ...prev,
      { role: "user", kind: "image", url, name: file.name, focusQuestion },
    ]);
    runImage(file, focusQuestion);
  }
  async function runImage(file, focusQuestion = "") {
    lastRequestRef.current = { kind: "image", file, focusQuestion };
    setVerificationProgress({ done: false, pct: 10, elapsedS: 0, logs: [] });
    setLoading(true);
    try {
      await inspectImageFile(file);
      setVerificationProgress({ done: false, pct: 45, elapsedS: 0, logs: [] });

      const result = await analyzeImage(file, { conversationId, focusQuestion });
      setVerificationProgress({ done: false, pct: 85, elapsedS: 0, logs: [] });

      // 실제 OCR 완료 체크를 인지할 수 있도록 짧게 유지한 뒤 결과를 표시한다.
      await new Promise((resolve) => setTimeout(resolve, 550));
      handleResult(result, focusQuestion);
    } catch (err) {
      handleError(err);
    } finally {
      setLoading(false);
    }
  }

  // ── 입력 전송 ──
  function handleSend() {
    if (loading) return;
    if (pendingClarification) {
      const answer = input.trim();
      if (!answer) return;
      setInput("");
      if (answer === "취소") {
        setPendingClarification(null);
        lastRequestRef.current = null;
        setMessages((prev) => [
          ...prev,
          { role: "assistant", kind: "text", text: "추가 정보 입력을 취소했습니다." },
        ]);
        return;
      }
      const question = pendingClarification.question || {};
      if (question.input_mode === "DATE" && !isValidArticleDate(answer)) {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            kind: "clarification_request",
            text: "실제 달력에 존재하는 YYYY-MM-DD 형식으로 다시 알려주세요. 취소하려면 '취소'를 입력하세요.",
          },
        ]);
        return;
      }
      const article = pendingClarification;
      const clarificationAnswer = {
        question_id: question.id,
        role: question.role,
        value: answer,
      };
      const clarificationAnswers = [
        ...(article.clarificationAnswers || []),
        clarificationAnswer,
      ];
      setPendingClarification(null);
      setMessages((prev) => [...prev, { role: "user", kind: "text", text: answer }]);
      runText(article.text, {
        inputType: article.inputType,
        focusQuestion: article.focusQuestion,
        title: article.title,
        date: article.date || "",
        dateSource: article.dateSource || null,
        clarificationAnswers,
        requestConversationId: article.conversationId,
      });
      return;
    }
    if (pendingImage) {
      const file = pendingImage.file;
      const focusQuestion = sourceQuestion.trim();
      setInput("");
      setSourceQuestion("");
      clearPendingImage();
      verifyImage(file, focusQuestion);
      return;
    }
    if (!input.trim()) return;
    const text = input;
    setInput("");
    const trimmed = text.trim();
    const inputType = /^https?:\/\/\S+$/i.test(trimmed)
      ? "url"
      : trimmed.length >= 400 || trimmed.split(/\r?\n/).length >= 3
        ? "article"
        : "auto";
    const focusQuestion = sourceQuestion.trim();
    setSourceQuestion("");
    verifyText(text, { inputType, focusQuestion });
  }
  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }
  // 오류 뒤 '다시 시도' — 사용자 말풍선을 다시 만들지 않고 마지막 요청만 재실행한다.
  function retryLast() {
    const last = lastRequestRef.current;
    if (!last || loading) return;
    if (last.kind === "image") runImage(last.file, last.focusQuestion);
    else runText(last.text, {
      inputType: last.inputType,
      focusQuestion: last.focusQuestion,
      title: last.title,
      date: last.date,
      dateSource: last.dateSource,
      clarificationAnswers: last.clarificationAnswers || [],
      requestConversationId: last.requestConversationId,
    });
  }
  // 범위 밖 안내에서 예시 칩을 누르면 그 질문을 새로 전송한다.
  function handleRecoveryPick(example) {
    if (loading) return;
    verifyText(example, { inputType: "auto", focusQuestion: "" });
  }
  function pickImage(file) {
    if (!file || !file.type?.startsWith("image/")) return;
    if (pendingImage?.url) URL.revokeObjectURL(pendingImage.url);
    setPendingImage({ file, url: URL.createObjectURL(file) });
  }
  function clearPendingImage() {
    if (pendingImage?.url) URL.revokeObjectURL(pendingImage.url);
    setPendingImage(null);
    if (fileRef.current) fileRef.current.value = "";
  }

  const canSend = pendingImage != null || input.trim().length > 0;
  const trimmedInput = input.trim();
  const draftSourceType = pendingImage
    ? "image"
    : /^https?:\/\/\S+$/i.test(trimmedInput)
      ? "url"
      : trimmedInput.length >= 400 || trimmedInput.split(/\r?\n/).length >= 3
        ? "article"
        : "";

  return (
    <div className="chat-app">
      <div
        ref={chatBodyRef}
        className="chat-body"
        onScroll={(e) => onScroll?.(e.currentTarget.scrollTop > 24)}
      >
        {messages.map((msg, i) => {
          if (msg.kind === "progress") {
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant">
                  <ProgressBubble progress={msg.progress} />
                </div>
              </div>
            );
          }
          if (msg.kind === "article") {
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant result-bubble">
                  <ArticleResult segments={msg.segments} />
                </div>
              </div>
            );
          }
          if (msg.kind === "document") {
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant result-bubble">
                  <ArticleDocumentResult
                    document={msg.document}
                    extraction={msg.extraction}
                    focusQuestion={msg.focusQuestion}
                  />
                </div>
              </div>
            );
          }
          if (msg.kind === "clarification_request" || msg.kind === "date_request") {
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant">{msg.text}</div>
              </div>
            );
          }
          if (msg.role === "user" && msg.kind === "image") {
            return (
              <div key={i} className="c-row user">
                <div className="c-bubble user c-image-bubble">
                  <img src={msg.url} alt={msg.name || "첨부 이미지"} />
                  <span className="c-image-caption">
                    <ImageIcon /> {msg.name}
                  </span>
                  {msg.focusQuestion && <span className="c-image-focus">확인할 내용 · {msg.focusQuestion}</span>}
                </div>
              </div>
            );
          }
          if (msg.role === "user") {
            if (msg.kind === "source") {
              return (
                <div key={i} className="c-row user">
                  <SourceInputBubble text={msg.text} sourceType={msg.sourceType} focusQuestion={msg.focusQuestion} />
                </div>
              );
            }
            return (
              <div key={i} className="c-row user">
                <UserTextBubble text={msg.text} />
              </div>
            );
          }
          if (msg.kind === "notice") {
            const isLast = i === messages.length - 1;
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant c-notice">
                  <span>{msg.text}</span>
                  {isLast && (
                    <RecoveryChips
                      examples={RECOVERY_EXAMPLES}
                      onPick={handleRecoveryPick}
                      disabled={loading}
                    />
                  )}
                </div>
              </div>
            );
          }
          if (msg.kind === "error") {
            const isLast = i === messages.length - 1;
            const canRetry = isLast && lastRequestRef.current != null;
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant c-error">
                  <span className="c-error-icon" aria-hidden="true">
                    <AlertIcon />
                  </span>
                  <div className="c-error-body">
                    <span>{msg.text}</span>
                    {canRetry && (
                      <button
                        type="button"
                        className="c-retry-btn"
                        onClick={retryLast}
                        disabled={loading}
                      >
                        <RefreshIcon size="1em" />
                        <span>다시 시도</span>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          }
          return (
            <div key={i} className="c-row assistant">
              <div className="c-bubble assistant">{msg.text}</div>
            </div>
          );
        })}

        {loading && (
          <div className="c-row assistant">
            <div className="c-bubble assistant c-loading"><ProgressBubble progress={verificationProgress} /></div>
          </div>
        )}
      </div>

      <div className="chat-input-wrap">
        {pendingImage && (
          <div className="chat-attach">
            <img src={pendingImage.url} alt="첨부 미리보기" />
            <span className="chat-attach-name">{pendingImage.file.name}</span>
            <button
              className="chat-attach-remove"
              onClick={clearPendingImage}
              aria-label="이미지 제거"
            >
              ✕
            </button>
          </div>
        )}
        {draftSourceType && (
          <label className="chat-source-question">
            <span>우선 확인할 내용 <small>선택</small></span>
            <input
              value={sourceQuestion}
              onChange={(e) => setSourceQuestion(e.target.value)}
              placeholder={draftSourceType === "image"
                ? "예: 이미지의 그래프 수치가 맞는지 확인해 주세요"
                : "예: 기사에서 언급한 취업자 증가 폭이 맞나요?"}
            />
          </label>
        )}
        <div className="chat-input">
          <button
            className="chat-attach-btn"
            onClick={() => fileRef.current?.click()}
            title="이미지 첨부"
            aria-label="이미지 첨부"
          >
            <ImageIcon size="1.3em" />
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) pickImage(f);
            }}
          />
          <textarea
            className="c-textarea"
            placeholder={
              pendingClarification
                ? pendingClarification.question?.input_mode === "DATE"
                  ? "기사 발행일 YYYY-MM-DD 입력 또는 '취소'"
                  : "추가 통계 조건을 입력하거나 '취소'"
                : pendingImage
                ? "이미지가 첨부되었습니다"
                : "통계 질문, 기사 URL 또는 본문 입력 후 Enter"
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            disabled={pendingImage != null}
          />
          <button
            className={`c-send ${loading ? "is-loading" : canSend ? "is-ready" : ""}`}
            onClick={handleSend}
            disabled={loading || !canSend}
          >
            <span>{loading ? "분석 중" : "전송"}</span>
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChatApp;
