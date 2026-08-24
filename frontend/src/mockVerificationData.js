// UI/UX 확인용 고정 목업 데이터입니다.
// 하나의 실제 2025년 기사를 URL 입력과 본문 입력 시나리오로 나눴습니다.
// 기사 전문을 복제하지 않고 기사에 보도된 사실을 요약·재구성했습니다.

const ARTICLE = {
  publisher: "연합뉴스",
  title: "7월 취업자 17만명 늘었지만…음식점·농림 '내수고용' 한파(종합2보)",
  publishedAt: "2025-08-13T11:38:00+09:00",
  url: "https://www.yna.co.kr/view/AKR20250813022252002",
  usageNote: "실제 기사에 보도된 통계 사실을 UI 검증용 문장으로 요약·재구성",
};

const ARTICLE_BODY_INPUT = `2025년 7월 고용동향에 따르면 15세 이상 취업자는 2,902만9천 명으로 전년 동월보다 17만1천 명 증가했다. 전체 고용률은 63.4%로 0.1%p 상승했고, 15~64세 고용률은 70.2%로 0.4%p 높아졌다.

산업별로는 제조업 취업자가 7만8천 명, 건설업 취업자가 9만2천 명 감소했다. 반면 보건업 및 사회복지서비스업 취업자는 26만3천 명 증가했다. 연령별로는 60세 이상 취업자가 34만2천 명, 30대 취업자가 9만3천 명 늘었지만 청년층 취업자는 15만8천 명 줄었다. 청년층 고용률은 45.8%로 전년 동월보다 0.7%p 하락했다.

실업자는 72만6천 명으로 1만1천 명 감소했고 실업률은 2.4%였다. 일이나 구직활동을 하지 않은 20대 '쉬었음' 인구는 42만1천 명으로 7월 기준 가장 큰 규모를 기록했다.`;

const CLAIMS = [
  {
    id: "employed-total",
    text: "2025년 7월 15세 이상 취업자는 2,902만9천 명이다.",
    verdict: "match",
    officialValue: "2,902만9천 명",
    explanation: "공식 고용동향의 취업자 29,029천 명과 일치합니다.",
  },
  {
    id: "employed-change",
    text: "취업자는 전년 동월보다 17만1천 명 증가했다.",
    verdict: "match",
    officialValue: "17만1천 명 증가",
    explanation: "공식 통계의 전년 동월 대비 171천 명 증가와 일치합니다.",
  },
  {
    id: "employment-rate",
    text: "15세 이상 고용률은 63.4%다.",
    verdict: "match",
    officialValue: "63.4%",
    explanation: "기사 수치와 2025년 7월 공식 고용률이 같습니다.",
  },
  {
    id: "employment-rate-oecd",
    text: "15~64세 고용률은 70.2%다.",
    verdict: "match",
    officialValue: "70.2%",
    explanation: "OECD 비교 기준 공식 고용률과 일치합니다.",
  },
  {
    id: "youth-employed-change",
    text: "청년층 취업자는 전년 동월보다 15만8천 명 감소했다.",
    verdict: "match",
    officialValue: "15만8천 명 감소",
    explanation: "15~29세 취업자 증감 수치가 공식 통계와 일치합니다.",
  },
  {
    id: "youth-employment-rate",
    text: "청년층 고용률은 45.8%다.",
    verdict: "match",
    officialValue: "45.8%",
    explanation: "15~29세 청년층의 공식 고용률과 일치합니다.",
  },
  {
    id: "unemployment-rate",
    text: "실업률은 2.4%다.",
    verdict: "match",
    officialValue: "2.4%",
    explanation: "2025년 7월 공식 실업률과 일치합니다.",
  },
  {
    id: "twenties-resting",
    text: "20대 '쉬었음' 인구는 42만1천 명이다.",
    verdict: "match",
    officialValue: "42만1천 명",
    explanation: "기사와 공식 고용동향에 제시된 규모가 같습니다.",
  },
];

const EVIDENCE = {
  organization: "통계청",
  tableName: "2025년 7월 고용동향",
  period: "2025년 07월",
  href: "https://mods.go.kr/board.es?act=view&bid=210&list_no=438054&mid=a10301030100",
};

const MOCK_TIMING = {
  totalMs: 8000,
  stepWeights: [0.18, 0.32, 0.22, 0.28],
};

function buildProcess(sourceType) {
  return [
    sourceType === "url"
      ? { id: "fetch", label: "기사 본문 추출", status: "completed", note: "실제 기사 URL에서 제목·날짜·본문 확인" }
      : { id: "read", label: "기사 본문 해석", status: "completed", note: "입력된 기사 본문과 문단 구조 확인" },
    { id: "extract", label: "수치 주장 추출", status: "completed", note: "검증 가능한 주장 8개 감지" },
    { id: "compare", label: "공식 통계 대조", status: "completed", note: "2025년 7월 고용동향과 비교" },
  ];
}

export const VERIFICATION_MOCKS = [
  {
    id: "mock-2025-news-url",
    isMock: true,
    basedOnRealArticle: true,
    scenario: "article_url",
    timing: MOCK_TIMING,
    article: ARTICLE,
    input: {
      sourceType: "url",
      label: "기사 URL 입력",
      icon: "link",
      display: ARTICLE.url,
      raw: ARTICLE.url,
      focusQuestion: "기사에 나온 2025년 7월 고용 수치가 맞는지 확인해 주세요.",
    },
    process: buildProcess("url"),
    extraction: {
      title: ARTICLE.title,
      publisher: ARTICLE.publisher,
      publishedDate: "2025년 08월 13일",
      claimCount: CLAIMS.length,
    },
    summary: {
      headline: "실제 기사 수치 8개 · 모두 일치",
      detail: "실제 기사 URL에서 확인한 고용 수치를 2025년 7월 공식 고용동향과 대조했습니다.",
      counts: { match: 8, mismatch: 0, unverifiable: 0 },
    },
    claims: CLAIMS,
    evidence: EVIDENCE,
  },
  {
    id: "mock-2025-news-body",
    isMock: true,
    basedOnRealArticle: true,
    scenario: "article_body",
    timing: MOCK_TIMING,
    article: ARTICLE,
    input: {
      sourceType: "article",
      label: "기사 본문 입력",
      icon: "document",
      display: "2025년 7월 취업자는 2,902만9천 명으로 전년 동월보다 17만1천 명 증가했다…",
      raw: ARTICLE_BODY_INPUT,
      focusQuestion: "기사 본문에 나온 고용 수치를 모두 검증해 주세요.",
    },
    process: buildProcess("article"),
    extraction: {
      title: ARTICLE.title,
      publisher: ARTICLE.publisher,
      characterCount: ARTICLE_BODY_INPUT.length,
      claimCount: CLAIMS.length,
    },
    summary: {
      headline: "기사 본문 주장 8개 · 모두 일치",
      detail: "실제 기사를 요약·재구성한 본문에서 수치 주장을 추출해 공식 통계와 대조했습니다.",
      counts: { match: 8, mismatch: 0, unverifiable: 0 },
    },
    claims: CLAIMS,
    evidence: EVIDENCE,
  },
];

// 랜딩의 예시를 실제 채팅 결과 UI에서 바로 열기 위한 변환기입니다.
export function mockToDisplayMessages(mock) {
  const isSource = ["article", "url"].includes(mock.input.sourceType);
  const userMessage = isSource
    ? {
        role: "user",
        kind: "source",
        text: mock.input.raw,
        sourceType: mock.input.sourceType,
        focusQuestion: mock.input.focusQuestion || "",
      }
    : { role: "user", kind: "text", text: mock.input.raw };

  const segments = mock.claims.flatMap((claim, index) => [
    ...(index > 0 ? [{ id: `spacer-${index}`, text: "\n\n", verifiable: false }] : []),
    {
      id: claim.id,
      text: claim.text,
      verifiable: true,
      verdict: claim.verdict,
      answer: claim.explanation,
      calc: `공식 통계: ${claim.officialValue}`,
      table: {
        name: mock.evidence.tableName,
        href: mock.evidence.href,
        path: `${mock.evidence.organization} · ${mock.evidence.period}`,
      },
    },
  ]);

  return [
    userMessage,
    {
      role: "assistant",
      kind: "text",
      text: `${mock.article.publisher} 「${mock.article.title}」의 수치 검증을 완료했습니다. ${mock.summary.detail}`,
    },
    { role: "assistant", kind: "article", segments, isMock: true },
  ];
}

function normalizeMockInput(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

// URL 또는 준비된 본문을 직접 붙여 넣으면 서버 대신 해당 목업을 반환합니다.
export function findVerificationMock(rawInput) {
  const normalized = normalizeMockInput(rawInput);
  return VERIFICATION_MOCKS.find(
    (mock) => normalizeMockInput(mock.input.raw) === normalized,
  );
}

export { ARTICLE_BODY_INPUT };
