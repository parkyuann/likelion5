"""Adjudicate which KOSIS table answers a routed value.

This produces the gold CLAUDE.md P0 requires before any retrieval number may
be called recall.  The person sees the value, the fields the layers produced,
and ten lexical candidates; they pick one, or reject all ten.

Rejecting all ten has to be as cheap as accepting one.  If the tool made
``없음`` awkward the gold would drift toward whatever the candidate generator
happened to surface, and the generator would end up grading itself.  The same
reason is why the candidate list carries no default selection.

``표 없음`` and ``후보 밖에 있음`` are separate answers on purpose: the first says
KOSIS does not hold this statistic, the second says it does but this generator
missed it.  Collapsing them would hide exactly the number this exercise
exists to produce.
"""

from __future__ import annotations

import argparse
import json
import shutil
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

MATCH_STATUSES = (
    "후보에서 찾음",
    "후보 밖에 있음",
    "표 없음",
    "판단 보류",
)
NEEDS_TABLE = "후보에서 찾음"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "retrieval_gold.html"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class AdjudicationStore:
    def __init__(self, working_path: Path) -> None:
        self.working_path = working_path
        self.rows = read_jsonl(working_path)
        self.index = {
            str(row["target_id"]): position
            for position, row in enumerate(self.rows)
        }

    def save(self, target_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("gold_match_status") or "")
        if status and status not in MATCH_STATUSES:
            raise ValueError(f"unknown status: {status}")
        row = self.rows[self.index[target_id]]
        table_key = str(payload.get("gold_table_key") or "")
        if status == NEEDS_TABLE and not table_key:
            raise ValueError("후보에서 찾음 requires a table")
        by_rank = {
            str(candidate["table_key"]): candidate
            for candidate in row.get("candidates") or []
        }
        chosen = by_rank.get(table_key)
        # A table typed in by hand is recorded, but it can only mean the
        # generator missed it — otherwise the rank would be known.
        if table_key and chosen is None and status == NEEDS_TABLE:
            raise ValueError("선택한 표가 후보 목록에 없습니다")
        row["gold_match_status"] = status
        row["gold_table_key"] = table_key
        row["gold_tbl_name"] = (
            chosen["tbl_name"] if chosen
            else str(payload.get("gold_tbl_name") or "")
        )
        row["gold_from_candidate_rank"] = chosen["rank"] if chosen else ""
        row["adjudication_note"] = str(payload.get("adjudication_note") or "")
        row["review_status"] = "검토완료" if status else "미검토"
        write_jsonl(self.working_path, self.rows)
        return row

    def progress(self) -> dict[str, Any]:
        done = [row for row in self.rows if row.get("gold_match_status")]
        found = [
            row for row in done
            if row["gold_match_status"] == NEEDS_TABLE
        ]
        return {
            "total": len(self.rows),
            "done": len(done),
            "remaining": len(self.rows) - len(done),
            "statuses": dict(
                Counter(row["gold_match_status"] for row in done)
            ),
            # Rank distribution is the whole point: it is what a later dense
            # retriever has to beat, and it is only meaningful once every
            # target is adjudicated.
            "found_at_rank": dict(
                Counter(
                    row["gold_from_candidate_rank"] for row in found
                    if row.get("gold_from_candidate_rank")
                )
            ),
        }


def build_handler(store: AdjudicationStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # noqa: A003
            return

        def _send(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                body = TEMPLATE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/rows":
                self._send({
                    "rows": store.rows, "statuses": list(MATCH_STATUSES),
                })
                return
            if self.path == "/api/progress":
                self._send(store.progress())
                return
            self._send({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            from urllib.parse import unquote

            if not self.path.startswith("/api/rows/"):
                self._send({"error": "not found"}, 404)
                return
            target_id = unquote(self.path.rsplit("/", 1)[-1])
            if target_id not in store.index:
                self._send({"error": f"unknown row {target_id}"}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            try:
                row = store.save(target_id, payload)
            except ValueError as exc:
                self._send({"error": str(exc)}, 400)
                return
            self._send({"row": row, "progress": store.progress()})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--working", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.working.exists():
        args.working.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source, args.working)
    store = AdjudicationStore(args.working)
    server = HTTPServer(("127.0.0.1", args.port), build_handler(store))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"retrieval gold adjudication: {url}")
    print(f"작업 파일 (자동 저장): {args.working}")
    print(f"진행: {store.progress()}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n중지했습니다. 저장된 작업 파일은 그대로 유지됩니다.")


if __name__ == "__main__":
    main()
