// ── 검증 기록 저장소 (데모용 localStorage) ─────────────
// 백엔드 연결 시 loadHistory/addHistory를 서버 조회/저장으로 교체.
const HISTORY_KEY = "kosis_history";
const MAX = 50; // 최근 50건까지 보관

export function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
  } catch {
    return [];
  }
}

// record: { article, segments }  →  id/ts는 여기서 부여
export function addHistory({ article, segments }) {
  const list = loadHistory();
  const record = {
    id:
      (typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : String(Date.now())),
    ts: Date.now(),
    article,
    segments,
  };
  const next = [record, ...list].slice(0, MAX); // 최신순
  localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
  return record;
}

export function clearHistory() {
  localStorage.removeItem(HISTORY_KEY);
}
