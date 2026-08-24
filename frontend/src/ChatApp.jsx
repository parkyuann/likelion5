import { useState, useRef, useEffect } from "react";
import "./ChatApp.css";
import { analyzeInput, analyzeImage, ApiError } from "./api.js";
import { ImageIcon, PinIcon, AlertIcon, DocIcon, LinkIcon, CheckIcon } from "./icons.jsx";
import { findVerificationMock, mockToDisplayMessages } from "./mockVerificationData.js";

// ── KOSIS 통계표 주소 ──────────────────────────────────
function kosisTableUrl(orgId, tblId) {
  return `https://kosis.kr/statHtml/statHtml.do?orgId=${orgId}&tblId=${tblId}`;
}

// KOSIS 원문을 열기 전에 근거의 핵심 메타데이터를 빠르게 확인합니다.
function EvidenceLink({ table }) {
  const href = table.href || kosisTableUrl(table.orgId, table.tblId);
  const tooltipId = table.orgId && table.tblId
    ? `evidence-${table.orgId}-${table.tblId}`
    : undefined;
  return (
    <span className="c-evidence-link-wrap">
      <a
        className="c-table"
        href={href}
        target="_blank"
        rel="noreferrer"
        aria-describedby={tooltipId}
      >
        <span className="c-table-link-icon" aria-hidden="true">🔗</span>
        <span>{table.name}</span>
      </a>
      <span
        className="c-link-preview"
        id={tooltipId}
        role="tooltip"
      >
        <span className="c-link-preview-label">KOSIS 공식 근거</span>
        <strong>{table.name}</strong>
        {table.path && <span>{table.path}</span>}
        {table.orgId && table.tblId && (
          <span className="c-link-preview-meta">
            기관 {table.orgId} · 통계표 {table.tblId}
          </span>
        )}
      </span>
    </span>
  );
}

const VERDICTS = {
  match: { label: "일치", className: "match" },
  mismatch: { label: "불일치", className: "mismatch" },
  notfound: { label: "검증 불가능 · 매칭 실패", className: "unverifiable" },
  outofscope: { label: "검증 불가능 · 대상 밖", className: "unverifiable" },
};

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
          <EvidenceLink table={seg.table} />
          {seg.table.path && (
            <span className="c-path">
              <PinIcon /> {seg.table.path}
            </span>
          )}
        </span>
      )}
      <Candidates candidates={seg.candidates} />
    </span>
  );
}

// ── 결과: 기사 전체 + 클릭 판정 ──────────────────────────
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
        <div className="c-summary-heading">
          <strong>문장 검증 결과</strong>
          <span>검증된 문장을 선택하면 판정 근거를 확인할 수 있습니다.</span>
        </div>
        <div className="c-summary-metrics">
          <span className="c-summary-item match">
            <span>일치</span>
            <strong className="c-summary-number">{counts.match}</strong>
          </span>
          <span className="c-summary-item mismatch">
            <span>불일치</span>
            <strong className="c-summary-number">{counts.mismatch}</strong>
          </span>
          <span className="c-summary-item unverifiable">
            <span>검증 불가능</span>
            <strong className="c-summary-number">{counts.unverifiable}</strong>
          </span>
        </div>
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
  if (typeof window === "undefined") return null;
  const seconds = Number(new URLSearchParams(window.location.search).get("mockDelay"));
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  return Math.min(60, Math.max(1, seconds)) * 1000;
}

function VerificationProgress({ progress }) {
  const sourceType = progress.sourceType || "auto";
  const steps = SOURCE_LOADING_STEPS[sourceType] || SOURCE_LOADING_STEPS.auto;
  const completed = new Set(progress.completed || []);
  const completedCount = completed.size;
  const percent = Math.round((completedCount / steps.length) * 100);
  const activeIndex = Math.min(progress.active ?? 0, steps.length - 1);
  return (
    <div className="c-verification-progress">
      <div className="c-progress-head">
        <span className="c-progress-orbit" aria-hidden="true" />
        <div>
          <small className="c-progress-kicker">검증 중</small>
          <strong>{steps[activeIndex]}</strong>
          <span>{completedCount} / {steps.length}단계 완료 · {percent}%</span>
        </div>
      </div>
      <div className="c-progress-rail" aria-hidden="true">
        <span style={{ width: `${percent}%` }} />
      </div>
      <div className="c-progress-steps">
        {steps.map((label, index) => (
          <span
            key={label}
            className={completed.has(index) ? "done" : index === activeIndex ? "active" : ""}
          >
            <i>{completed.has(index) ? "✓" : index + 1}</i>{label}
          </span>
        ))}
      </div>
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
  const [loading, setLoading] = useState(false);
  const chatBodyRef = useRef(null);
  const fileRef = useRef(null);
  const mockTimersRef = useRef(new Set());
  const startedRef = useRef(false);
  const [verificationProgress, setVerificationProgress] = useState({
    sourceType: "auto",
    completed: [],
    active: 0,
  });
  const onMessagesChangeRef = useRef(onMessagesChange);

  useEffect(() => {
    onMessagesChangeRef.current = onMessagesChange;
  }, [onMessagesChange]);

  useEffect(() => () => {
    mockTimersRef.current.forEach((timerId) => clearTimeout(timerId));
    mockTimersRef.current.clear();
  }, []);

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
        runMockVerification(initial.mock, {
          focusQuestion: initial.mock.input.focusQuestion,
        });
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
    return new Promise((resolve) => {
      const timerId = setTimeout(() => {
        mockTimersRef.current.delete(timerId);
        resolve();
      }, ms);
      mockTimersRef.current.add(timerId);
    });
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

  async function runText(text, {
    inputType = "auto",
    focusQuestion = "",
  } = {}) {
    const matchedMock = findVerificationMock(text);
    if (matchedMock) {
      // 사용자 말풍선은 이미 추가됐고, 목업도 단계별 로딩 후 결과를 표시합니다.
      await runMockVerification(matchedMock, {
        focusQuestion,
      });
      return;
    }
    setVerificationProgress({ sourceType: inputType, completed: [], active: 0 });
    setLoading(true);
    try {
      handleResult(
        await analyzeInput(text, { conversationId, inputType, focusQuestion }),
        focusQuestion,
      );
    } catch (err) {
      handleError(err);
    } finally {
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
    setVerificationProgress({ sourceType: "image", completed: [], active: 0 });
    setLoading(true);
    try {
      await inspectImageFile(file);
      setVerificationProgress({ sourceType: "image", completed: [0], active: 1 });

      const result = await analyzeImage(file, { conversationId, focusQuestion });
      setVerificationProgress({ sourceType: "image", completed: [0, 1], active: 2 });

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
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant c-notice">{msg.text}</div>
              </div>
            );
          }
          if (msg.kind === "error") {
            return (
              <div key={i} className="c-row assistant">
                <div className="c-bubble assistant c-error">
                  <span className="c-error-icon" aria-hidden="true">
                    <AlertIcon />
                  </span>
                  <div className="c-error-body">
                    <span>{msg.text}</span>
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
            <div className="c-bubble assistant c-loading"><VerificationProgress progress={verificationProgress} /></div>
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
              pendingImage
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
