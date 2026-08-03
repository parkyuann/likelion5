"""Manual KOSIS retrieval probe.

Answers one question the structuring layer cannot answer about itself: how
wrong can the fields be before a KOSIS table stops being findable.  The Gate B
field threshold was going to be a guess without it.

Scope is deliberately small — a person searches KOSIS by hand for ~20 claims
and records what happened.  No retrieval pipeline is built and no catalog work
happens here, so CLAUDE.md 6.7절's gate on KOSIS 검색 is not being pre-empted;
this measures the next stage's tolerance in order to calibrate this one.
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

# The system retrieves Top-K and reranks; it never scans every hit.  So the
# question is whether a usable table surfaces near the top, not whether a
# person can locate the single correct one among hundreds.  A query that
# returns many undifferentiated tables is itself a finding — the indicator is
# too broad to retrieve with — so it gets its own verdict instead of being
# forced into 찾음 or 못찾음.
VERDICTS = ("찾음", "후보 과다", "못찾음", "애매")
TOP_N_TO_CHECK = 20
BLOCKING_FIELDS = (
    "indicator 표현",
    # A weekly claim against a monthly table cannot be verified, and no amount
    # of dimension alignment fixes it — unlike a dimension mismatch, which the
    # retrieval stage resolves against table metadata.
    "period 단위 불일치",
    "period 값 없음",
    "measurement",
    "population",
    "item",
    "dimension",
    "표 자체가 없음",
    "기타",
)
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "retrieval_probe.html"


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


# Quantifier modifiers that open a Korean noun phrase without naming anything
# a statistical table is titled after.  `전체 국세 수입` broadened to `전체`
# retrieves nothing about taxes; broadened to `국세 수입` it retrieves the
# right family.
GENERIC_LEADERS = ("전체", "총", "전국", "국내", "주요", "누적", "약")


def broad_terms(indicator: str) -> list[str]:
    """Shorten an indicator from the left, dropping empty quantifiers first."""
    tokens = indicator.split()
    while tokens and tokens[0] in GENERIC_LEADERS:
        tokens = tokens[1:]
    if not tokens:
        return []
    out = []
    if len(tokens) >= 2:
        out.append(" ".join(tokens[:2]))
    out.append(tokens[0])
    return out


def search_terms(row: dict[str, Any]) -> list[str]:
    """Build progressively looser queries so a miss can be localised.

    If the full indicator finds nothing but the bare item does, the indicator
    phrasing was the obstacle — that is exactly what the probe needs to know.
    """
    terms: list[str] = []
    indicator = str(row.get("indicator") or "").strip()
    if indicator:
        terms.append(indicator)
    for values in (row.get("item") or [], row.get("population") or []):
        for value in values:
            text = str(value).strip()
            if text and text not in terms:
                terms.append(text)
    for text in broad_terms(indicator):
        if text not in terms:
            terms.append(text)
    return terms


class ProbeStore:
    def __init__(self, working_path: Path) -> None:
        self.working_path = working_path
        self.rows = read_jsonl(working_path)
        for row in self.rows:
            # Derived, never human input — recompute so a fix to the query
            # rules reaches rows that were already loaded once.
            row["search_terms"] = search_terms(row)
            row.setdefault("blocking_fields", [])
            row.setdefault("found_via", "")
            row.setdefault("tried_terms", [])
            row.setdefault("custom_terms", [])
        self.index = {
            str(row["probe_id"]): position
            for position, row in enumerate(self.rows)
        }

    def save(self, probe_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        verdict = str(payload.get("kosis_table_found") or "")
        if verdict and verdict not in VERDICTS:
            raise ValueError(f"unknown verdict: {verdict}")
        blocking = [
            str(value) for value in payload.get("blocking_fields") or []
            if str(value) in BLOCKING_FIELDS
        ]
        row = self.rows[self.index[probe_id]]
        # Which query worked localises the fault to a layer.  The full
        # indicator missing where a bare item hits means L4 put something in
        # the indicator that no table title carries — a rule to fix, not a
        # query to tune.  Terms outside this row's own list are dropped so the
        # tally stays comparable across rows.
        # A term the person typed themselves is kept, not dropped: "none of
        # the generated queries worked but this one did" is the sharpest
        # evidence that the fields are wrong, and losing it would leave the
        # row looking like an ordinary miss.  It is tallied apart from the
        # generated ranks, which stay comparable across rows.
        offered = row.get("search_terms") or []
        custom = [
            text for text in (
                str(value).strip() for value in payload.get("custom_terms") or []
            )
            if text and text not in offered
        ]
        allowed = set(offered) | set(custom)
        tried = [
            str(value) for value in payload.get("tried_terms") or []
            if str(value) in allowed
        ]
        found_via = str(payload.get("found_via") or "")
        row["kosis_table_found"] = verdict
        row["kosis_table_name"] = str(payload.get("kosis_table_name") or "")
        row["blocking_fields"] = blocking
        row["found_via"] = found_via if found_via in allowed else ""
        row["tried_terms"] = tried
        row["custom_terms"] = custom
        row["probe_note"] = str(payload.get("probe_note") or "")
        write_jsonl(self.working_path, self.rows)
        return row

    def progress(self) -> dict[str, Any]:
        done = sum(1 for row in self.rows if row.get("kosis_table_found"))
        by_bucket: dict[str, Counter] = {}
        for row in self.rows:
            if row.get("kosis_table_found"):
                by_bucket.setdefault(row.get("bucket", "?"), Counter())[
                    row["kosis_table_found"]
                ] += 1
        return {
            "total": len(self.rows),
            "done": done,
            "remaining": len(self.rows) - done,
            "verdicts": dict(
                Counter(
                    row["kosis_table_found"] for row in self.rows
                    if row.get("kosis_table_found")
                )
            ),
            "by_bucket": {k: dict(v) for k, v in by_bucket.items()},
            "blocking_fields": dict(
                Counter(
                    field for row in self.rows
                    for field in row.get("blocking_fields") or []
                )
            ),
            # By rank, not by text: term 1 is always the full indicator, so
            # "found at rank 2+" counts the rows whose indicator was too
            # specific to retrieve with.
            "found_via_rank": dict(
                Counter(
                    (row.get("search_terms") or []).index(row["found_via"]) + 1
                    for row in self.rows
                    if row.get("found_via") in (row.get("search_terms") or [])
                )
            ),
            # Rows only a hand-typed query could reach: the generated fields
            # failed as a query even though a table existed.
            "found_via_custom": [
                {"probe_id": row["probe_id"],
                 "indicator": row.get("indicator", ""),
                 "found_via": row["found_via"]}
                for row in self.rows
                if row.get("found_via")
                and row["found_via"] not in (row.get("search_terms") or [])
            ],
        }


def build_handler(store: ProbeStore) -> type[BaseHTTPRequestHandler]:
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
                    "rows": store.rows,
                    "verdicts": list(VERDICTS),
                    "blocking_fields": list(BLOCKING_FIELDS),
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
            probe_id = unquote(self.path.rsplit("/", 1)[-1])
            if probe_id not in store.index:
                self._send({"error": f"unknown row {probe_id}"}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            try:
                row = store.save(probe_id, payload)
            except ValueError as exc:
                self._send({"error": str(exc)}, 400)
                return
            self._send({"row": row, "progress": store.progress()})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--working", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.working.exists():
        args.working.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source, args.working)
    store = ProbeStore(args.working)
    server = HTTPServer(("127.0.0.1", args.port), build_handler(store))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"retrieval probe: {url}")
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
