"""Keyboard-driven labeller for routing gold.

One value at a time, one keystroke per decision.  The L2 labeller needed
drag-and-click because it captured structure; this one captures a single
three-way judgement, so the interaction collapses to a keypress and the screen
carries only what that judgement needs.

Model predictions are never shown: this labelling produces the gold that
grades the model.
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

CLASSES = ("KOSIS_CANDIDATE", "OUT_OF_SCOPE", "NOT_CLAIM")
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "routing_gold.html"


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


class RoutingStore:
    def __init__(self, working_path: Path) -> None:
        self.working_path = working_path
        self.rows = read_jsonl(working_path)
        self.index = {
            str(row["judgement_id"]): position
            for position, row in enumerate(self.rows)
        }

    def payload(self) -> dict[str, Any]:
        return {"classes": list(CLASSES), "rows": self.rows}

    def sentence_ids(self, judgement_id: str) -> list[str]:
        """Every judgement in the same sentence, in reading order.

        79% of holdout-1 values share a sentence with another value, and a
        sentence's values almost always take the same class — ``26.2%에서
        18.6%로`` is one judgement written twice.  Grouping halves the
        decisions without touching the sample, which is the only lever here
        that does not cost statistical power.
        """
        row = self.rows[self.index[judgement_id]]
        key = (row.get("article_idx"), row.get("sentence_id"))
        return [
            str(other["judgement_id"]) for other in self.rows
            if (other.get("article_idx"), other.get("sentence_id")) == key
        ]

    def save(
        self,
        judgement_id: str,
        judged_class: str,
        note: str,
    ) -> dict[str, Any]:
        if judged_class and judged_class not in CLASSES:
            raise ValueError(f"unknown class: {judged_class}")
        row = self.rows[self.index[judgement_id]]
        row["judged_class"] = judged_class
        row["judge_note"] = note
        row["review_status"] = "검토완료" if judged_class else "미검토"
        write_jsonl(self.working_path, self.rows)
        return row

    def save_sentence(
        self,
        judgement_id: str,
        judged_class: str,
        note: str,
    ) -> list[dict[str, Any]]:
        """Apply one judgement to every value in the sentence.

        Values already judged individually are overwritten on purpose: the
        person is looking at the whole sentence when they press the key, so
        the keystroke is a statement about all of it.  Correcting one value
        afterwards uses the single-value endpoint.
        """
        return [
            self.save(other_id, judged_class, note)
            for other_id in self.sentence_ids(judgement_id)
        ]

    def progress(self) -> dict[str, Any]:
        done = sum(1 for row in self.rows if row.get("judged_class"))
        return {
            "total": len(self.rows),
            "done": done,
            "remaining": len(self.rows) - done,
            "classes": dict(
                Counter(
                    row["judged_class"] for row in self.rows
                    if row.get("judged_class")
                )
            ),
        }


def build_handler(store: RoutingStore) -> type[BaseHTTPRequestHandler]:
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
                self._send(store.payload())
                return
            if self.path == "/api/progress":
                self._send(store.progress())
                return
            self._send({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/rows/"):
                self._send({"error": "not found"}, 404)
                return
            judgement_id = self.path.rsplit("/", 1)[-1]
            from urllib.parse import unquote

            judgement_id = unquote(judgement_id)
            if judgement_id not in store.index:
                self._send({"error": f"unknown row {judgement_id}"}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            judged = str(payload.get("judged_class") or "")
            note = str(payload.get("judge_note") or "")
            try:
                if payload.get("scope") == "sentence":
                    rows = store.save_sentence(judgement_id, judged, note)
                else:
                    rows = [store.save(judgement_id, judged, note)]
            except (ValueError, KeyError) as exc:
                self._send({"error": str(exc)}, 400)
                return
            self._send({
                "row": rows[0], "rows": rows, "progress": store.progress(),
            })

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--working", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.working.exists():
        args.working.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source, args.working)
    store = RoutingStore(args.working)
    server = HTTPServer(("127.0.0.1", args.port), build_handler(store))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"routing gold labeller: {url}")
    print(f"작업 파일 (자동 저장): {args.working}")
    print(f"진행: {store.progress()}")
    print("종료하려면 Ctrl+C")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n중지했습니다. 저장된 작업 파일은 그대로 유지됩니다.")


if __name__ == "__main__":
    main()
