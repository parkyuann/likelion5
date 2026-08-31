import { useEffect, useRef, useState } from "react";
import "./Landing.css";
import { ImageIcon, DocIcon, LinkIcon, QuestionIcon } from "./icons.jsx";

const VERIFICATION_EXAMPLES = [
  {
    input: { label: "통계 질의", icon: "question", display: "2024년 서울 청년 실업률이 전국보다 높았나요?" },
    summary: { headline: "서울이 전국보다 낮음", detail: "같은 연도와 연령 기준으로 비교했습니다." },
    evidence: {
      tableName: "지역별 청년층 실업률",
      href: "https://kosis.kr/search/search.do?query=2024%EB%85%84%20%EC%B2%AD%EB%85%84%20%EC%8B%A4%EC%97%85%EB%A5%A0%20%EC%84%9C%EC%9A%B8",
    },
  },
  {
    input: { label: "비교 질문", icon: "question", display: "지난해 1인 가구 비중이 처음으로 35%를 넘었나요?" },
    summary: { headline: "공식 통계로 확인", detail: "연도별 비중과 최초 초과 시점을 확인했습니다." },
    evidence: {
      tableName: "인구총조사·가구원수별 가구",
      href: "https://kosis.kr/search/search.do?query=1%EC%9D%B8%20%EA%B0%80%EA%B5%AC%20%EB%B9%84%EC%A4%91",
    },
  },
  {
    input: { label: "기사 본문", icon: "document", display: "…7월 취업자는 전년 동월보다 17만1천 명 늘었다. 청년층 고용률은 45.8%로…" },
    summary: { headline: "수치 주장 2개 확인", detail: "발췌문에서 검증 가능한 문장을 찾아 대조했습니다." },
    evidence: {
      tableName: "경제활동인구조사·고용동향",
      href: "https://kosis.kr/search/search.do?query=%EA%B3%A0%EC%9A%A9%EB%8F%99%ED%96%A5%20%EC%B7%A8%EC%97%85%EC%9E%90",
    },
  },
  {
    input: { label: "기사 URL", icon: "link", display: "https://www.yna.co.kr/view/AKR20250813022252002" },
    summary: { headline: "기사 본문에서 수치 추출", detail: "본문을 읽고 고용 관련 주장을 문장별로 확인했습니다." },
    evidence: {
      tableName: "경제활동인구조사·2025년 7월 고용동향",
      href: "https://kosis.kr/search/search.do?query=2025%EB%85%84%207%EC%9B%94%20%EA%B3%A0%EC%9A%A9%EB%8F%99%ED%96%A5",
    },
  },
];

function WorkflowIcon({ type }) {
  if (type === "link") return <LinkIcon />;
  if (type === "image") return <ImageIcon />;
  if (type === "document") return <DocIcon />;
  return <QuestionIcon />;
}

function isImageFile(file) {
  return file && file.type && file.type.startsWith("image/");
}

function splitArticleAndQuestion(value, explicitQuestion) {
  if (explicitQuestion.trim()) {
    return { source: value.trim(), question: explicitQuestion.trim() };
  }
  const lines = value.split(/\r?\n/);
  const last = (lines.at(-1) || "").trim();
  const labeled = last.match(/^(?:질문|질의|확인할 내용)\s*[:：]\s*(.+)$/);
  const looksLikeQuestion = last.length > 0 && last.length <= 180 && /[?？]$/.test(last);
  if ((labeled || looksLikeQuestion) && lines.length > 1) {
    return {
      source: lines.slice(0, -1).join("\n").trim(),
      question: (labeled?.[1] || last).trim(),
    };
  }
  return { source: value.trim(), question: "" };
}

// 첫 화면: 통계 질문·기사 URL·본문 텍스트 또는 이미지(캡처)를 입력받습니다.
function Landing({ onSubmit }) {
  const [text, setText] = useState("");
  const [image, setImage] = useState(null); // { file, url }
  const [focusQuestion, setFocusQuestion] = useState("");
  const [dragging, setDragging] = useState(false);
  const [demoIndex, setDemoIndex] = useState(0);
  const [renderDemo, setRenderDemo] = useState(true);
  const [demoLeaving, setDemoLeaving] = useState(false);
  const areaRef = useRef(null);
  const fileRef = useRef(null);
  const previewRef = useRef(null);

  const normalizedText = text.trim();
  const detectedSourceType = image
    ? "image"
    : /^https?:\/\/\S+$/i.test(normalizedText)
      ? "url"
      : normalizedText.length >= 180 || normalizedText.split(/\r?\n/).length >= 3
        ? "article"
        : "";
  const canSubmit = image != null || normalizedText.length > 0;
  const showDemo = !image && text.trim().length === 0 && focusQuestion.trim().length === 0;
  const demo = VERIFICATION_EXAMPLES[demoIndex];

  useEffect(() => {
    if (showDemo) {
      setRenderDemo(true);
      setDemoLeaving(false);
      return undefined;
    }
    setDemoLeaving(true);
    const timer = setTimeout(() => {
      setRenderDemo(false);
      setDemoLeaving(false);
    }, 680);
    return () => clearTimeout(timer);
  }, [showDemo]);

  // 이미지가 붙여넣어지거나 첨부되면 미리보기로 포커스를 옮긴다. 따라서 첫 화면에서도
  // Enter 한 번으로 바로 검증을 시작할 수 있다.
  useEffect(() => {
    if (image) previewRef.current?.focus();
  }, [image]);

  function changeDemo(direction) {
    setDemoIndex((current) =>
      (current + direction + VERIFICATION_EXAMPLES.length) % VERIFICATION_EXAMPLES.length
    );
  }

  function submit() {
    const question = focusQuestion.trim();
    if (image) {
      return onSubmit({ image: image.file, focusQuestion: question, inputType: "image" });
    }
    if (!text.trim()) return;
    const value = text.trim();
    const separated = detectedSourceType === "article"
      ? splitArticleAndQuestion(value, question)
      : { source: value, question };
    const inputType = /^https?:\/\/\S+$/i.test(separated.source)
        ? "url"
        : detectedSourceType === "article"
          ? "article"
          : "auto";
    return onSubmit({ text: separated.source, focusQuestion: separated.question, inputType });
  }

  function handleKeyDown(e) {
    // Enter = 전송, Shift+Enter = 줄바꿈
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function pickImage(file) {
    if (!isImageFile(file)) return;
    if (image?.url) URL.revokeObjectURL(image.url);
    setImage({ file, url: URL.createObjectURL(file) });
    setDemoIndex(1);
  }
  function clearImage() {
    if (image?.url) URL.revokeObjectURL(image.url);
    setImage(null);
    setFocusQuestion("");
    if (fileRef.current) fileRef.current.value = "";
  }

  function onDrop(e) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) pickImage(file);
  }
  function onPaste(e) {
    if (e.defaultPrevented) return;
    const file = [...(e.clipboardData?.items || [])]
      .map((it) => (it.kind === "file" ? it.getAsFile() : null))
      .find(isImageFile);
    if (file) {
      e.preventDefault();
      pickImage(file);
    }
  }

  return (
    <div className="landing">
      <div className="landing-inner">
        <header className="landing-header">
          <span className="landing-badge">국가통계 기반 팩트체크</span>
          <h1 className="landing-title">
            뉴스 속 수치,
            <br />
            <span className="landing-title-accent">국가통계로 검증</span>하세요
          </h1>
          <p className="landing-sub">
            기사 본문이나 캡처 이미지를 넣으면, 문장별 수치를 KOSIS 국가통계와
            대조해
            <br />
            근거 통계표까지 찾아드립니다.
          </p>
        </header>

        <div
          className={`landing-workbench ${renderDemo ? "" : "is-inputting"} ${demoLeaving ? "is-transitioning" : ""}`}
        >
          <div
            className={`landing-card ${dragging ? "dragging" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onPaste={onPaste}
          >
          <div className="landing-composer-head">
            <div>
              <strong>검증할 내용을 입력하세요</strong>
              <span>질문·기사·URL·이미지를 자동으로 구분합니다</span>
            </div>
          </div>
          {image ? (
            <div
              ref={previewRef}
              className="image-preview"
              tabIndex={0}
              role="group"
              aria-label="첨부한 이미지. Enter 키를 누르면 검증을 시작합니다."
              onKeyDown={handleKeyDown}
            >
              <img src={image.url} alt="첨부 이미지 미리보기" />
              <div className="image-preview-meta">
                <span className="image-preview-name">{image.file.name}</span>
                <span className="image-preview-hint">
                  이미지 속 수치를 OCR로 읽어 검증합니다 · Enter로 시작
                </span>
              </div>
              <button
                className="image-remove"
                onClick={clearImage}
                aria-label="이미지 제거"
              >
                ✕
              </button>
            </div>
          ) : (
            <textarea
              ref={areaRef}
              className="landing-textarea"
              aria-label="검증할 통계 질문, 기사 URL 또는 기사 본문"
              placeholder="통계 질문, 기사 URL 또는 기사 본문을 입력하세요"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={onPaste}
              rows={2}
            />
          )}

          {detectedSourceType && (
            <label className="landing-focus-question">
              <span>이 자료에서 확인할 내용 <small>선택</small></span>
              <input
                value={focusQuestion}
                onChange={(e) => setFocusQuestion(e.target.value)}
                placeholder="예: 기사에서 언급한 청년 고용률이 맞나요?"
              />
              <small>비워 두면 자료 안의 모든 검증 가능한 수치를 확인합니다.</small>
            </label>
          )}

          <div className="landing-actions">
            <div className="landing-actions-left">
              <button
                className="attach-btn"
                onClick={() => fileRef.current?.click()}
                type="button"
                title="이미지 첨부"
                aria-label="이미지 첨부"
              >
                <ImageIcon size="1.35em" />
                <span>이미지 첨부</span>
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
              <span className="landing-attach-hint">질문과 근거 자료는 분리해 처리합니다</span>
            </div>
            <div className="landing-actions-right">
              <button
                className={`landing-btn ${canSubmit ? "is-ready" : ""}`}
                onClick={submit}
                disabled={!canSubmit}
              >
                <span>검증하기</span>
              </button>
            </div>
          </div>

            {dragging && (
              <div className="drop-hint" aria-hidden="true">
                여기에 이미지를 놓으세요
              </div>
            )}
          </div>

          {renderDemo && (
            <aside className={`landing-demo ${demoLeaving ? "is-leaving" : ""}`} aria-label="검증 예시">
              <div className="landing-demo-head">
                <span className="landing-demo-kicker">검증 예시</span>
                <div className="landing-demo-head-right">
                  <div className="landing-demo-type"><WorkflowIcon type={demo.input.icon} /> {demo.input.label}</div>
                  <div className="landing-demo-nav" aria-label="검증 예시 선택">
                    <button
                      type="button"
                      onClick={() => changeDemo(-1)}
                      aria-label="이전 검증 예시"
                    >
                      ←
                    </button>
                    <span>{demoIndex + 1} / {VERIFICATION_EXAMPLES.length}</span>
                    <button
                      type="button"
                      onClick={() => changeDemo(1)}
                      aria-label="다음 검증 예시"
                    >
                      →
                    </button>
                  </div>
                </div>
              </div>
              <div className="landing-demo-content" key={demoIndex}>
                <div className="landing-demo-input">
                  <small>예시 입력</small>
                  <strong>{demo.input.display}</strong>
                </div>
                <div className="landing-demo-result">
                  <small>예시 출력</small>
                  <strong>{demo.summary.headline}</strong>
                  <span>{demo.summary.detail}</span>
                </div>
                <a
                  className="landing-demo-source"
                  href={demo.evidence.href}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span className="demo-source-icon" aria-hidden="true">
                    <LinkIcon />
                  </span>
                  <span>
                    <small>KOSIS 공식 근거</small>
                    <strong>{demo.evidence.tableName}</strong>
                  </span>
                  <span className="demo-source-arrow" aria-hidden="true">↗</span>
                </a>
              </div>
            </aside>
          )}
        </div>

      </div>
    </div>
  );
}

export default Landing;
