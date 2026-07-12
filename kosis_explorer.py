"""KOSIS 통계목록 API에서 조회 가능한 통계표를 탐색한다.

외부 패키지 없이 동작하며, 프로젝트 루트의 .env에서 KOSIS_API_KEY를 읽는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://kosis.kr/openapi/statisticsList.do"
DEFAULT_ENV_FILE = Path(__file__).with_name(".env")

VIEW_CODES = {
    "MT_ZTITLE": "국내통계 주제별",
    "MT_OTITLE": "국내통계 기관별",
    "MT_GTITLE01": "e-지방지표(주제별)",
    "MT_GTITLE02": "e-지방지표(지역별)",
    "MT_CHOSUN_TITLE": "광복이전통계(1908~1943)",
    "MT_HANKUK_TITLE": "대한민국통계연감",
    "MT_STOP_TITLE": "작성중지통계",
    "MT_RTITLE": "국제통계",
    "MT_BUKHAN": "북한통계",
    "MT_TM1_TITLE": "대상별통계",
    "MT_TM2_TITLE": "이슈별통계",
    "MT_ETITLE": "영문 KOSIS",
}


def load_env(path: Path) -> None:
    """간단한 KEY=VALUE 형식의 .env를 환경변수에 적재한다."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def fetch_list(api_key: str, view: str, parent: str) -> list[dict[str, Any]]:
    params = {
        "method": "getList",
        "apiKey": api_key,
        "vwCd": view,
        "parentListId": parent,
        "format": "json",
        "jsonVD": "Y",
    }
    request = Request(
        f"{API_URL}?{urlencode(params)}",
        headers={"User-Agent": "kosis-data-explorer/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = json.loads(response.read().decode(charset))

    if isinstance(payload, dict):
        # 오류 응답도 JSON 객체로 올 수 있으므로 메시지를 그대로 보여준다.
        message = payload.get("errMsg") or payload.get("message") or str(payload)
        raise RuntimeError(f"KOSIS API 오류: {message}")
    if not isinstance(payload, list):
        raise RuntimeError("예상하지 못한 KOSIS API 응답 형식입니다.")
    return payload


def collect_tree(
    api_key: str, view: str, parent: str, depth: int
) -> list[dict[str, Any]]:
    rows = fetch_list(api_key, view, parent)
    if depth <= 0:
        return rows

    result: list[dict[str, Any]] = []
    for row in rows:
        enriched = {**row, "_PARENT_ID": parent}
        result.append(enriched)
        child_id = row.get("LIST_ID")
        if child_id:
            result.extend(collect_tree(api_key, view, str(child_id), depth - 1))
    return result


def display(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows, 1):
        if row.get("TBL_ID"):
            label = row.get("TBL_NM", "(이름 없음)")
            detail = f"기관={row.get('ORG_ID', '-')} / 표={row['TBL_ID']}"
            kind = "통계표"
        else:
            label = row.get("LIST_NM", "(이름 없음)")
            detail = f"목록ID={row.get('LIST_ID', '-')}"
            kind = "목록"
        print(f"{index:>4}. [{kind}] {label} ({detail})")
    print(f"\n총 {len(rows):,}개 항목")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KOSIS에서 조회 가능한 통계 목록 탐색")
    parser.add_argument("--view", default="MT_ZTITLE", choices=VIEW_CODES)
    parser.add_argument("--parent", default="A", help="시작 목록 ID (기본값: A)")
    parser.add_argument(
        "--depth", type=int, default=0, help="하위 목록 재귀 조회 깊이 (기본값: 0)"
    )
    parser.add_argument("--keyword", help="목록명/통계표명에 포함된 검색어")
    parser.add_argument("--save", type=Path, help="전체 응답을 저장할 JSON 경로")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--list-views", action="store_true", help="서비스뷰 코드 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_views:
        for code, name in VIEW_CODES.items():
            print(f"{code:<18} {name}")
        return 0
    if args.depth < 0:
        print("오류: --depth는 0 이상이어야 합니다.", file=sys.stderr)
        return 2

    load_env(args.env_file)
    api_key = os.getenv("KOSIS_API_KEY", "").strip()
    if not api_key:
        print(
            f"오류: {args.env_file}에 KOSIS_API_KEY를 설정해주세요.",
            file=sys.stderr,
        )
        return 2

    try:
        rows = collect_tree(api_key, args.view, args.parent, args.depth)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as error:
        print(f"조회 실패: {error}", file=sys.stderr)
        return 1

    if args.keyword:
        keyword = args.keyword.casefold()
        rows = [
            row
            for row in rows
            if keyword
            in str(row.get("TBL_NM") or row.get("LIST_NM") or "").casefold()
        ]

    display(rows)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"저장 완료: {args.save.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
