import { useEffect } from "react";
import "./Intro.css";

// 인트로: 손바닥이 위에서 "탁" 내려오는 정지 제스처 → 워드마크 → 페이드아웃 → onDone
function Intro({ onDone }) {
  useEffect(() => {
    const t = setTimeout(onDone, 3200);
    return () => clearTimeout(t);
  }, [onDone]);

  return (
    <div className="intro" role="presentation">
      <div className="intro-data-field" aria-hidden="true">
        <span className="intro-scan" />
        <svg className="intro-chart" viewBox="0 0 900 520" preserveAspectRatio="xMidYMid slice">
          <path className="intro-chart-line intro-chart-line-soft" d="M0 390 C90 360 142 414 220 346 S354 278 432 315 S566 238 650 264 S790 174 900 198" />
          <path className="intro-chart-line" d="M0 405 C90 375 142 429 220 361 S354 293 432 330 S566 253 650 279 S790 189 900 213" />
          <g className="intro-chart-points">
            <circle cx="220" cy="361" r="5" />
            <circle cx="432" cy="330" r="5" />
            <circle cx="650" cy="279" r="5" />
            <circle cx="900" cy="213" r="5" />
          </g>
        </svg>
        <span className="intro-data-node intro-data-node-a"><small>공식 통계</small><strong>0.72</strong></span>
        <span className="intro-data-node intro-data-node-b"><small>데이터 대조</small><strong>98.4%</strong></span>
        <span className="intro-data-node intro-data-node-c"><small>KOSIS</small><strong>확인 중</strong></span>
      </div>
      <div className="intro-stage">
        <div className="intro-hand" aria-hidden="true">
          <span className="intro-shock" />
          <svg
            className="intro-palm"
            viewBox="0 0 24 24"
            width="108"
            height="108"
            fill="none"
            stroke="#587f92"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M8 13v-7.5a1.5 1.5 0 0 1 3 0v6.5" />
            <path d="M11 5.5v-2a1.5 1.5 0 1 1 3 0v8.5" />
            <path d="M14 5.5a1.5 1.5 0 0 1 3 0v6.5" />
            <path d="M17 7.5a1.5 1.5 0 0 1 3 0v8.5a6 6 0 0 1 -6 6h-2h.208a6 6 0 0 1 -5.012 -2.7a69.74 69.74 0 0 1 -.196 -.3c-.312 -.479 -1.407 -2.388 -3.286 -5.728a1.5 1.5 0 0 1 .536 -2.022a1.867 1.867 0 0 1 2.28 .28l1.47 1.47" />
          </svg>
        </div>
        <div className="intro-word">
          뉴스 오보 <span className="intro-word-accent">멈춰!</span>
        </div>
      </div>
    </div>
  );
}

export default Intro;
