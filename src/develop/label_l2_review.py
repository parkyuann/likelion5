"""Interactive helper for L2 contract v3 human review.

Writes the review JSONL directly, so no JSON has to be typed into Excel.
Span text is validated the moment it is entered: text that is absent from the
sentence or ambiguous is rejected and re-prompted instead of being stored.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from .l2_span_resolver import (
        SpanResolutionError,
        parse_value_candidate_span_ids,
        resolve_span,
    )
except ImportError:  # direct script execution
    from l2_span_resolver import (  # type: ignore[no-redef]
        SpanResolutionError,
        parse_value_candidate_span_ids,
        resolve_span,
    )

SUBTYPES = ("공식집계", "민간조사", "정책목표", "잠정추산", "법정기준")
HUMAN_ID_START = 90


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _used_ids(context_rows, human_rows, article_idx, kind):
    key = "scope_id" if kind == "scope" else "region_id"
    field = "indicator_scopes_json" if kind == "scope" else "source_regions_json"
    used = {
        str(r.get(key) or "")
        for r in context_rows
        if str(r.get("article_idx")) == article_idx and r.get(key)
    }
    for row in human_rows:
        if str(row.get("article_idx")) != article_idx:
            continue
        raw = row.get(field)
        if not raw:
            continue
        try:
            for entry in json.loads(raw):
                if entry.get(key):
                    used.add(str(entry[key]))
        except (json.JSONDecodeError, TypeError):
            continue
    return used


def _next_id(context_rows, human_rows, article_idx, kind):
    prefix = "SC" if kind == "scope" else "R"
    used = _used_ids(context_rows, human_rows, article_idx, kind)
    number = HUMAN_ID_START
    while f"{article_idx}-{prefix}{number:02d}" in used:
        number += 1
    return f"{article_idx}-{prefix}{number:02d}"


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def _ask_span(sentence: str, label: str) -> dict[str, Any] | None:
    """Return {'source_span_text', ...} or None when skipped."""
    while True:
        text = _ask(f"  {label} (원문에서 복사, 빈칸=건너뜀): ")
        if not text:
            return None
        try:
            resolve_span(sentence, text)
            return {"source_span_text": text}
        except SpanResolutionError as exc:
            message = str(exc)
            if "ambiguous" not in message:
                print(f"    ✗ {message}")
                continue
            index = _ask("    같은 표현이 여러 번 나옵니다. 몇 번째? (0부터): ")
            try:
                resolve_span(sentence, text, index)
            except SpanResolutionError as inner:
                print(f"    ✗ {inner}")
                continue
            return {"source_span_text": text, "occurrence_index": int(index)}


def _menu(title: str, options: list[str], extra: dict[str, str]) -> str | None:
    print(f"  {title}")
    for number, option in enumerate(options, start=1):
        print(f"    {number}. {option}")
    for key, description in extra.items():
        print(f"    {key}. {description}")
    while True:
        choice = _ask("  선택: ")
        if choice in extra:
            return choice
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("    ✗ 잘못된 선택")


def _region_choices(context_rows, human_rows, article_idx, sentence_id):
    """Region IDs defined earlier in the same article, with their sentence."""
    choices = []
    for row in context_rows:
        if str(row.get("article_idx")) != article_idx:
            continue
        if int(row.get("sentence_id") or 0) > sentence_id:
            continue
        if row.get("region_id"):
            choices.append((
                str(row["region_id"]),
                f"s{row.get('sentence_id')} {str(row.get('text') or '')[:40]}",
            ))
    for row in human_rows:
        if str(row.get("article_idx")) != article_idx:
            continue
        if int(row.get("sentence_id") or 0) > sentence_id:
            continue
        raw = row.get("source_regions_json")
        if not raw:
            continue
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for entry in entries:
            if entry.get("region_id"):
                choices.append((
                    str(entry["region_id"]),
                    f"s{row.get('sentence_id')} "
                    f"{entry.get('source_subtype')} "
                    f"{str(row.get('text') or '')[:34]}",
                ))
    seen, unique = set(), []
    for region_id, description in choices:
        if region_id not in seen:
            seen.add(region_id)
            unique.append((region_id, description))
    return unique


def _show(row, context_rows):
    article_idx = str(row.get("article_idx"))
    sentence_id = int(row.get("sentence_id") or 0)
    print("\n" + "=" * 78)
    print(f"[{row['sentence_review_id']}]  {row.get('review_reason')}")
    print(f"기사 {article_idx} · 문장 {sentence_id} · 발행 {row.get('published_at')}")
    print(f"제목: {row.get('title')}")
    print("-" * 78)
    for context in context_rows:
        if str(context.get("article_idx")) != article_idx:
            continue
        distance = int(context.get("sentence_id") or 0) - sentence_id
        if not -3 <= distance <= 1:
            continue
        marker = ">>" if distance == 0 else "  "
        tag = ""
        if context.get("region_id"):
            tag = f"  [{context.get('region_id')} {context.get('source_subtype')}]"
            if context.get("indicator_label"):
                tag += f" {context.get('indicator_label')}"
        print(f"{marker} s{context.get('sentence_id')}: "
              f"{str(context.get('text') or '')[:66]}{tag}")
    print("-" * 78)
    print(f"본문: {row.get('text')}")
    if row.get("value_candidates_text"):
        print(f"값 후보: {row.get('value_candidates_text')}")
    print("=" * 78)


def _label_inheritance(row, context_rows, human_rows):
    choices = _region_choices(
        context_rows, human_rows, str(row["article_idx"]),
        int(row.get("sentence_id") or 0),
    )
    print("  이 문장을 지배하는 출처 영역은?")
    for number, (region_id, description) in enumerate(choices, start=1):
        print(f"    {number}. {region_id}  {description}")
    print("    n. 지배 없음")
    print("    s. 건너뜀")
    while True:
        choice = _ask("  선택: ")
        if choice == "s":
            return False
        if choice == "n":
            row["dominant_region_decision"] = "지배 없음"
            return True
        if choice.isdigit() and 1 <= int(choice) <= len(choices):
            row["dominant_region_decision"] = choices[int(choice) - 1][0]
            return True
        print("    ✗ 잘못된 선택")


def _label_regions(row, context_rows, human_rows, require_note):
    sentence = str(row.get("text") or "")
    regions = []
    while True:
        subtype = _menu(
            "출처 종류는?", list(SUBTYPES),
            {"d": "입력 끝내기", "s": "이 행 건너뜀"},
        )
        if subtype == "s":
            return False
        if subtype == "d":
            break
        span = _ask_span(sentence, "근거 표현")
        if span is None:
            print("    ✗ 근거 표현은 필요합니다")
            continue
        entry = {
            "region_id": _next_id(
                context_rows, human_rows, str(row["article_idx"]), "region",
            ),
            "source_subtype": subtype,
        }
        entry.update(span)
        regions.append(entry)
        row["source_regions_json"] = json.dumps(regions, ensure_ascii=False)
        print(f"    ✓ {entry['region_id']} {subtype}")
        if _ask("  더 추가? (y/N): ").lower() != "y":
            break
    if not regions:
        return False
    row["source_regions_json"] = json.dumps(regions, ensure_ascii=False)
    if require_note and not row.get("reviewer_note"):
        row["reviewer_note"] = _ask("  판단 이유(필수): ")
    return True


def _label_multi_indicator(row, context_rows, human_rows):
    sentence = str(row.get("text") or "")
    scopes = []
    while True:
        label = _ask("  지표명 (빈칸=입력 끝): ")
        if not label:
            break
        attribution = _menu(
            "이 지표는?", ["이 문장에서 도입", "앞에서 상속"], {"s": "취소"},
        )
        if attribution == "s":
            continue
        if attribution == "앞에서 상속":
            scope_id = _ask("  앞에서 정의된 scope_id: ")
            scopes.append({
                "scope_id": scope_id,
                "indicator_label": label,
                "attribution_type": "앞에서 상속",
            })
        else:
            span = _ask_span(sentence, "지표 근거 표현")
            if span is None:
                print("    ✗ 도입 지표에는 근거 표현이 필요합니다")
                continue
            entry = {
                "scope_id": _next_id(
                    context_rows, human_rows, str(row["article_idx"]), "scope",
                ),
                "indicator_label": label,
                "attribution_type": "이 문장에서 도입",
            }
            entry.update(span)
            scopes.append(entry)
        row["indicator_scopes_json"] = json.dumps(scopes, ensure_ascii=False)
        print(f"    ✓ {scopes[-1]['scope_id']} {label}")
    if not scopes:
        return False
    row["indicator_scopes_json"] = json.dumps(scopes, ensure_ascii=False)

    pairs = [
        (chunk.split("=", 1)[0].strip(), span_id)
        for chunk, span_id in zip(
            str(row.get("value_candidate_span_ids") or "").split("|"),
            parse_value_candidate_span_ids(row.get("value_candidate_span_ids")),
        )
    ]
    boundaries = []
    for value, span_id in pairs:
        print(f"\n  값 '{value}' 는 어느 지표?")
        for number, scope in enumerate(scopes, start=1):
            print(f"    {number}. {scope['scope_id']} {scope['indicator_label']}")
        print("    n. 해당 없음")
        while True:
            choice = _ask("  선택: ")
            if choice == "n":
                break
            if choice.isdigit() and 1 <= int(choice) <= len(scopes):
                boundaries.append({
                    "scope_id": scopes[int(choice) - 1]["scope_id"],
                    "boundary_type": "값",
                    "target_value_span_ids": [span_id],
                })
                break
            print("    ✗ 잘못된 선택")
    row["clause_value_boundaries_json"] = json.dumps(
        boundaries, ensure_ascii=False,
    )
    return True


def _label_period(row):
    if _ask("  기간 문맥을 기록할까요? (y/N): ").lower() != "y":
        return
    sentence = str(row.get("text") or "")
    raw = _ask("  원문 기간 표현: ")
    if not raw:
        return
    span = _ask_span(sentence, "기간 근거 표현")
    if span is None:
        return
    entry = {
        "period_raw": raw,
        "period_absolute": _ask("  절대 기간 (예: 2025.09): "),
        "published_at": row.get("published_at"),
    }
    entry.update(span)
    row["period_contexts_json"] = json.dumps([entry], ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--human-jsonl", type=Path, required=True)
    parser.add_argument("--context-jsonl", type=Path, required=True)
    parser.add_argument("--reason", help="한 종류만 작업 (예: CONTEXT_REGION_INHERITANCE)")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    human_rows = _read_jsonl(args.human_jsonl)
    context_rows = _read_jsonl(args.context_jsonl)
    pending = [
        row for row in human_rows
        if row.get("label_provenance") != "HUMAN_CONFIRMED"
        and (not args.reason or row.get("review_reason") == args.reason)
    ]
    if args.limit:
        pending = pending[:args.limit]
    print(f"남은 행: {len(pending)}  (q 입력 시 저장 후 종료)")

    done = 0
    for row in pending:
        _show(row, context_rows)
        reason = row.get("review_reason")
        if reason == "CONTEXT_REGION_INHERITANCE":
            filled = _label_inheritance(row, context_rows, human_rows)
        elif reason == "OUT_OF_SCOPE_SUBTYPE":
            filled = _label_regions(row, context_rows, human_rows, False)
        elif reason == "REGION_CONFLICT_RESOLUTION":
            filled = _label_regions(row, context_rows, human_rows, True)
        else:
            filled = _label_multi_indicator(row, context_rows, human_rows)
            if filled:
                _label_period(row)
        if filled:
            note = _ask("  메모 (선택): ")
            if note == "q":
                break
            if note:
                row["reviewer_note"] = note
            row["label_provenance"] = "HUMAN_CONFIRMED"
            row["review_status"] = "검토완료"
            done += 1
        else:
            row["review_status"] = "보류"
            if not row.get("reviewer_note"):
                row["reviewer_note"] = "건너뜀"
        _write_jsonl(args.human_jsonl, human_rows)
        print(f"  저장됨 ({done}건 완료)")

    remaining = sum(
        1 for row in human_rows
        if row.get("label_provenance") != "HUMAN_CONFIRMED"
    )
    print(f"\n완료 {done}건 · 미완료 {remaining}건")
    print("검증: python -m src.develop.validate_l2_review_ingest "
          f"--human-jsonl {args.human_jsonl} --context-jsonl {args.context_jsonl}")


if __name__ == "__main__":
    main()