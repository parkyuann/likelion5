import "./Home.css";
import { DocIcon, ChartIcon, CheckIcon, ArrowRightIcon } from "./icons.jsx";

const STEPS = [
  {
    Icon: DocIcon,
    title: "기사·수치 입력",
    desc: "기사 URL·본문이나 캡처 이미지를 넣습니다.",
  },
  {
    Icon: ChartIcon,
    title: "국가통계와 대조",
    desc: "문장 속 수치를 KOSIS 국가통계에서 찾아 맞춰봅니다.",
  },
  {
    Icon: CheckIcon,
    title: "문장별 판정 · 근거표",
    desc: "일치·불일치 판정과 근거 통계표를 보여드립니다.",
  },
];

// 첫 화면(브랜드 홈): 정체성 + 핵심 정보 + 시작 CTA
function Home({ onStart }) {
  return (
    <div className="home">
      <div className="home-inner">
        <section className="home-hero">
          <span className="landing-badge">국가통계 기반 팩트체크</span>
          <h1 className="home-title">
            뉴스 속 수치,
            <br />
            <span className="landing-title-accent">국가통계로 검증</span>하세요
          </h1>
          <p className="home-sub">
            뉴스 기사가 인용한 통계 수치를 국가통계포털(KOSIS)과 대조해,
            <br className="home-br" />
            문장 하나하나의 사실 여부와 근거를 확인해 드립니다.
          </p>
          <button className="home-cta" onClick={onStart}>
            검증 시작하기
            <ArrowRightIcon size="1.05em" />
          </button>
        </section>

        <section className="home-how">
          <span className="home-how-label">이렇게 검증해요</span>
          <div className="home-steps">
            {STEPS.map(({ Icon, title, desc }, i) => (
              <div className="home-step" key={title}>
                <span className="home-step-no">{i + 1}</span>
                <span className="home-step-icon" aria-hidden="true">
                  <Icon size="1.4em" />
                </span>
                <strong className="home-step-title">{title}</strong>
                <span className="home-step-desc">{desc}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

export default Home;
