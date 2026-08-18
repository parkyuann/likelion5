// 색 없는 모던 라인 아이콘 세트 (stroke=currentColor → 글자색 상속, 단색)
// viewBox 24, 크기는 CSS(.si-icon svg)에서 지정.
const base = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round",
  strokeLinejoin: "round",
};

// 브랜드 로고: 방패 + 체크 (그라데이션) — 검증/신뢰 컨셉
export function LogoMark() {
  return (
    <svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient
          id="kosisLogoGrad"
          x1="7"
          y1="3"
          x2="33"
          y2="37"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#5a76b0" />
          <stop offset="0.55" stopColor="#2e4370" />
          <stop offset="1" stopColor="#1a2740" />
        </linearGradient>
      </defs>
      {/* 방패 */}
      <path
        d="M20 3.2l12.5 4.4v9.4c0 8.2-5.3 14.2-12.5 16.8C12.8 31.2 7.5 25.2 7.5 17V7.6L20 3.2Z"
        fill="url(#kosisLogoGrad)"
      />
      {/* 광택 하이라이트 */}
      <path
        d="M20 3.2l12.5 4.4v9.4c0 .5-.02 1-.06 1.5C28 15 24.2 13.7 20 13.7S12 15 7.56 18.5C7.52 18 7.5 17.5 7.5 17V7.6L20 3.2Z"
        fill="#ffffff"
        fillOpacity="0.10"
      />
      {/* 체크 */}
      <path
        d="M14 20.2l4.1 4.1L26.4 15.6"
        stroke="#ffffff"
        strokeWidth="2.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function IconPlus() {
  return (
    <svg {...base}>
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

// 신문이 쌓인/접힌 형태 (검증 기록)
export function IconNewspaper() {
  return (
    <svg {...base}>
      <path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2" />
      <path d="M18 14h-8" />
      <path d="M15 18h-5" />
      <path d="M10 6h8v4h-8V6Z" />
    </svg>
  );
}

export function IconStar() {
  return (
    <svg {...base}>
      <path d="M12 3.5l2.6 5.27 5.82.85-4.21 4.1.99 5.79L12 16.77l-5.2 2.73.99-5.79-4.21-4.1 5.82-.85L12 3.5Z" />
    </svg>
  );
}

// 표(통계표 탐색)
export function IconTable() {
  return (
    <svg {...base}>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="3" y1="9" x2="21" y2="9" />
      <line x1="3" y1="15" x2="21" y2="15" />
      <line x1="12" y1="3" x2="12" y2="21" />
    </svg>
  );
}

export function IconUser() {
  return (
    <svg {...base}>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

export function IconSettings() {
  return (
    <svg {...base}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
    </svg>
  );
}
