// UI/UX 확인용 목업 데이터.
// 백엔드 없이도 새 검증 UX(진행 링 · 결과 카드 · 인라인 상세)를 확인할 수 있게,
// 기사 본문의 "숫자가 있는 문장"마다 아래 4가지 판정을 순서대로 배정한다.
// (일치 → 불일치 → 매칭 실패 → 대상 밖 → 반복)
// ?mock=1 일 때만 사용된다. 반환 형태는 실제 /v1/verify/develop 응답과 동일.

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

// 문장 분리 / 숫자 감지 (숫자 사이의 마침표로는 나누지 않음: 11,694,600.8 유지)
function splitSentences(text) {
  return text.split(/(?<=[.!?。\n])(?!\d)/);
}
function hasNumber(sentence) {
  return /\d/.test(sentence);
}

// 기사 본문 → 우리 UI 세그먼트 배열. 실제 /v1/verify/develop 응답과 동일 형태.
export function mockVerifyArticle(text) {
  const segments = splitSentences(text || "");
  let caseIdx = 0;
  const results = segments.map((sentence) => {
    if (sentence.trim() && hasNumber(sentence)) {
      const demo = DEMO_CASES[caseIdx % DEMO_CASES.length];
      caseIdx += 1;
      return { text: sentence, verifiable: true, ...demo };
    }
    return { text: sentence, verifiable: false };
  });
  return { type: "article", status: "ui_mock", live: false, results };
}

// 데모용 샘플 기사(4개 판정을 내용과도 맞게 구성)
export const MOCK_SAMPLE_ARTICLE =
  "2024년 고정자산의 생산자본스톡은 11,694,600.8십억 원으로 전년보다 3.93% 증가했다. " +
  "지난달 실업률은 5.2%로 나타났다. 반려동물 관련 가구당 지출은 약 15만 원이었다. " +
  "정부는 2030년까지 관련 산업이 3배 성장할 것으로 전망했다.";
