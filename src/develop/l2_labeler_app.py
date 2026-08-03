"""Local click-driven labeller for the L2 contract v3 review sheet.

Serves one sentence at a time.  The reviewer selects evidence text with the
mouse and clicks value chips; this process assembles the contract JSON,
allocates article-scoped IDs and validates every span before saving, so no
JSON, ID or character offset is ever typed by hand.

Run:
    python -m src.develop.l2_labeler_app --working <working.jsonl>
"""

from __future__ import annotations

import argparse
import json
import shutil
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

try:
    from .l2_label_assembly import (
        SpanResolutionError,
        assemble_review_row,
        existing_ids,
        region_choices,
        scope_choices,
    )
    from .l2_span_resolver import parse_value_candidate_span_ids
    from .validate_l2_review_ingest import validate_l2_review_ingest
except ImportError:  # pragma: no cover - direct script execution
    from l2_label_assembly import (  # type: ignore[no-redef]
        SpanResolutionError,
        assemble_review_row,
        existing_ids,
        region_choices,
        scope_choices,
    )
    from l2_span_resolver import parse_value_candidate_span_ids
    from validate_l2_review_ingest import validate_l2_review_ingest


HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "l2_labeler.html"


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


class LabelStore:
    """Holds review rows and persists every save to the working file."""

    def __init__(
        self,
        working_path: Path,
        context_path: Path,
        contract_path: Path,
    ) -> None:
        self.working_path = working_path
        self.rows = read_jsonl(working_path)
        self.context = read_jsonl(context_path)
        self.contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.index = {
            str(row.get("sentence_review_id")): position
            for position, row in enumerate(self.rows)
        }

    def payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract.get("contract_version"),
            "source_region_subtypes": self.contract.get(
                "source_region_subtypes", []
            ),
            "rows": [self.row_payload(row) for row in self.rows],
        }

    def row_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        article_idx = str(row.get("article_idx") or "")
        rendered = row.get("value_candidate_span_ids") or ""
        chips = []
        for chunk in str(rendered).split("|"):
            value, separator, span_id = chunk.rpartition("=")
            if separator and span_id.strip():
                chips.append({
                    "value": value.strip(),
                    "span_id": span_id.strip(),
                })
        return {
            "sentence_review_id": row.get("sentence_review_id"),
            "article_idx": article_idx,
            "sentence_id": row.get("sentence_id"),
            "title": row.get("title"),
            "published_at": row.get("published_at"),
            "text": row.get("text"),
            "review_reason": row.get("review_reason"),
            "value_chips": chips,
            "review_status": row.get("review_status"),
            "reviewer_note": row.get("reviewer_note"),
            "saved": {
                "indicator_scopes_json": row.get("indicator_scopes_json"),
                "source_regions_json": row.get("source_regions_json"),
                "period_contexts_json": row.get("period_contexts_json"),
                "clause_value_boundaries_json": row.get(
                    "clause_value_boundaries_json"
                ),
                "dominant_region_decision": row.get(
                    "dominant_region_decision"
                ),
            },
            "article_context": self.article_context(article_idx),
            "region_choices": region_choices(
                article_idx, self.context, self.rows
            ),
            "scope_choices": scope_choices(
                article_idx, self.context, self.rows
            ),
        }

    def article_context(self, article_idx: str) -> list[dict[str, Any]]:
        return [
            {
                "sentence_review_id": row.get("sentence_review_id"),
                "sentence_id": row.get("sentence_id"),
                "text": row.get("text"),
                "row_kind": row.get("row_kind"),
                "scope_id": row.get("scope_id"),
                "region_id": row.get("region_id"),
                "indicator_label": row.get("indicator_label"),
                "source_subtype": row.get("source_subtype"),
            }
            for row in self.context
            if str(row.get("article_idx") or "") == article_idx
        ]

    def save(self, review_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        position = self.index[review_id]
        row = self.rows[position]
        article_idx = str(row.get("article_idx") or "")
        others = [
            other for other in self.rows
            if str(other.get("sentence_review_id")) != review_id
        ]
        updated = assemble_review_row(
            row,
            decision,
            existing_ids(article_idx, self.context, others, "scope"),
            existing_ids(article_idx, self.context, others, "region"),
        )
        self.rows[position] = updated
        write_jsonl(self.working_path, self.rows)
        return self.row_payload(updated)

    def progress(self) -> dict[str, Any]:
        done = sum(
            1 for row in self.rows if row.get("review_status") == "검토완료"
        )
        return {
            "total": len(self.rows),
            "done": done,
            "remaining": len(self.rows) - done,
        }

    def validate(self) -> dict[str, Any]:
        try:
            return validate_l2_review_ingest(self.rows, self.context)
        except ValueError as exc:
            return {"status": "INVALID", "errors": str(exc).split("\n")}


def build_handler(store: LabelStore) -> type[BaseHTTPRequestHandler]:
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
            if self.path == "/api/validate":
                self._send(store.validate())
                return
            self._send({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/rows/"):
                self._send({"error": "not found"}, 404)
                return
            review_id = self.path.rsplit("/", 1)[-1]
            length = int(self.headers.get("Content-Length") or 0)
            decision = json.loads(self.rfile.read(length) or b"{}")
            if review_id not in store.index:
                self._send({"error": f"unknown row {review_id}"}, 404)
                return
            try:
                row = store.save(review_id, decision)
            except (SpanResolutionError, ValueError) as exc:
                self._send({"error": str(exc)}, 400)
                return
            self._send({"row": row, "progress": store.progress()})

    return Handler


def prepare_working_copy(source: Path, working: Path) -> Path:
    if not working.exists():
        working.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, working)
    return working


def main() -> None:
    evaluation = Path(
        "data/develop/article_hcx_holdout_20260729/evaluation"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=evaluation / "l2_review_human_98_contract_v3_20260730.jsonl",
    )
    parser.add_argument(
        "--working",
        type=Path,
        default=evaluation / "l2_review_human_98_working.jsonl",
    )
    parser.add_argument(
        "--context",
        type=Path,
        default=evaluation / "l2_review_context_117_contract_v3_20260730.jsonl",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=evaluation / "l2_review_contract_v3_20260730.json",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    working = prepare_working_copy(args.source, args.working)
    store = LabelStore(working, args.context, args.contract)
    server = HTTPServer(("127.0.0.1", args.port), build_handler(store))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"L2 labeller: {url}")
    print(f"작업 파일 (자동 저장): {working}")
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
