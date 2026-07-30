"""agent_chat.py — 실전2 단일 실행 챗봇 (이 파일 하나만 실행).

질의 입력창 → 표 자동검색(하이브리드) → 필요하면 재질의(입력창) / 아니면 넘어감
           → KOSIS 조회 → 표 연산 → 답변.  '종료'까지 반복.

지금까지 만든 실전2 부품(agent_map·agent_pipeline·agent_clarify·kosis_call_tool·table_ops)을
그대로 불러 잇기만 한다. 새 로직 없음 — 입출력(입력창)과 반복만 담당.

실행:  .\.venv\Scripts\python.exe src\agent_chat.py   (또는 VSCode ▶)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # src 경로 보장(직접 실행 대비)

from agent_pipeline import run


def _ask(question: str) -> str:
    """재질의가 필요할 때만 호출되는 입력창."""
    return input(f"  ↳ {question}\n  답> ").strip()


def main() -> None:
    print("=" * 60)
    print(" 실전2 통계 사실검증 챗봇")
    print(" 예) 고정자산 생산자본스톡이 2024년 전년보다 늘었나?")
    print(" 종료하려면: 종료 / quit / exit")
    print("=" * 60)
    while True:
        try:
            q = input("\n질의: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break
        if not q:
            continue
        if q.lower() in {"종료", "quit", "exit", "q"}:
            print("종료합니다.")
            break
        try:
            run(q, answer_fn=_ask, verbose=True, explain=True)   # 재질의는 _ask로, 답변은 DASH-002 자연어 설명
        except Exception as e:
            print(f"  [오류] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
