import "./Explore.css";

// KOSIS 통계표 탐색 — 우리 화면 안에 iframe으로 직접 띄운다.
// (index.do는 X-Frame-Options / CSP frame-ancestors / 프레임 탈출 스크립트가 없어 임베드 가능)
const KOSIS_URL = "https://kosis.kr/index/index.do";

function Explore() {
  return (
    <div className="explore">
      <div className="explore-bar">
        <span className="explore-title">KOSIS 통계표 탐색</span>
        <a
          className="explore-open"
          href={KOSIS_URL}
          target="_blank"
          rel="noreferrer"
        >
          새 탭에서 열기 ↗
        </a>
      </div>
      <div className="explore-frame-wrap">
        <iframe
          className="explore-frame"
          src={KOSIS_URL}
          title="KOSIS 통계표 탐색"
          referrerPolicy="no-referrer-when-downgrade"
        />
      </div>
    </div>
  );
}

export default Explore;
