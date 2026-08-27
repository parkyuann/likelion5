"""Live KOSIS metadata profiles for R4-C1 oracle-table resolution.

Only the official TBL, ITM, and PRD metadata endpoints are consumed.  The
module does not read catalog embeddings, B1 materialized profiles, cell data,
or gold query plans.  Every ITEM and every ordered dimension/value is retained
because KOSIS Param queries address dimensions as objL1..objL8.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from src.news_verification.runtime.kosis_catalog_crawler import KosisOpenAPI, get_api_key, safe_error_message


CONTRACT_VERSION = "r4c1-live-kosis-metadata-v2"
ENDPOINTS = ("TBL", "ITM", "PRD")
MAX_DIMENSIONS = 8


class LiveMetadataError(ValueError):
    """Raised when live metadata cannot prove a complete Param inventory."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _safe_table_key(table_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", table_key)


def split_table_key(table_key: str) -> tuple[str, str]:
    org_id, separator, tbl_id = str(table_key or "").partition(":")
    if not separator or not org_id or not tbl_id or ":" in tbl_id:
        raise LiveMetadataError(f"invalid table_key: {table_key!r}")
    return org_id, tbl_id


class _RecordingSession:
    """Capture exact HTTP response bytes without persisting request secrets."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last_response_bytes: bytes | None = None
        self.http_attempts = 0

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self.last_response_bytes = None
        self.http_attempts += 1
        response = self._inner.get(*args, **kwargs)
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            self.last_response_bytes = bytes(content)
        return response


def _ensure_recording_session(api: Any) -> _RecordingSession | None:
    session = getattr(api, "session", None)
    if isinstance(session, _RecordingSession):
        return session
    if session is None:
        return None
    recorder = _RecordingSession(session)
    api.session = recorder
    return recorder


def _unit_inventory(row: Mapping[str, Any]) -> dict[str, str]:
    """Preserve KOSIS unit fields on every ITM endpoint row.

    KOSIS may attach the effective series unit to a non-ITEM dimension value
    (for example a generic ``주요지표`` ITEM whose account values are measured
    in won, dollars, or percent).  Dropping those fields makes the selected
    series impossible to validate even though the official endpoint supplied
    the evidence.
    """

    return {
        "unit_id": str(row.get("UNIT_ID") or "").strip(),
        "unit_nm": str(row.get("UNIT_NM") or "").strip(),
        "unit_eng_nm": str(row.get("UNIT_ENG_NM") or "").strip(),
    }


def _ordered_inventory(rows: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    items: list[dict[str, str]] = []
    item_ids: set[str] = set()
    dimensions: dict[str, dict[str, Any]] = {}

    for row_index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise LiveMetadataError(f"ITM row {row_index} is not an object")
        obj_id = str(row.get("OBJ_ID") or "").strip()
        value_id = str(row.get("ITM_ID") or "").strip()
        label = str(row.get("ITM_NM") or "").strip()
        if not obj_id or not value_id or not label:
            raise LiveMetadataError(f"ITM row {row_index} misses OBJ_ID/ITM_ID/ITM_NM")
        if obj_id == "ITEM":
            if value_id in item_ids:
                raise LiveMetadataError(f"duplicate ITEM id: {value_id}")
            item_ids.add(value_id)
            items.append(
                {
                    "itm_id": value_id,
                    "itm_nm": label,
                    **_unit_inventory(row),
                }
            )
            continue

        obj_name = str(row.get("OBJ_NM") or "").strip()
        order_raw = str(row.get("OBJ_ID_SN") or "").strip()
        if not obj_name or not order_raw.isdigit():
            raise LiveMetadataError(f"dimension {obj_id} misses OBJ_NM or numeric OBJ_ID_SN")
        source_order = int(order_raw)
        if source_order < 1:
            raise LiveMetadataError(f"dimension source order is invalid: {obj_id}={source_order}")
        dimension = dimensions.setdefault(
            obj_id,
            {
                "obj_id": obj_id,
                "obj_nm": obj_name,
                "obj_id_sn": source_order,
                "values": [],
                "_value_ids": set(),
            },
        )
        if dimension["obj_nm"] != obj_name or dimension["obj_id_sn"] != source_order:
            raise LiveMetadataError(f"dimension metadata conflict: {obj_id}")
        if value_id in dimension["_value_ids"]:
            raise LiveMetadataError(f"duplicate dimension value id: {obj_id}:{value_id}")
        dimension["_value_ids"].add(value_id)
        dimension["values"].append(
            {
                "value_id": value_id,
                "value_name": label,
                "value_name_eng": str(row.get("ITM_NM_ENG") or "").strip(),
                **_unit_inventory(row),
            }
        )

    if not items:
        raise LiveMetadataError("ITM endpoint returned no ITEM rows")
    ordered = sorted(dimensions.values(), key=lambda row: (row["obj_id_sn"], row["obj_id"]))
    if not ordered:
        raise LiveMetadataError("ITM endpoint returned no DIMENSION rows")
    if len(ordered) > MAX_DIMENSIONS:
        raise LiveMetadataError(f"more than {MAX_DIMENSIONS} dimensions cannot map to Param API")
    source_orders = [row["obj_id_sn"] for row in ordered]
    if len(set(source_orders)) != len(source_orders):
        raise LiveMetadataError(f"dimension OBJ_ID_SN values are not unique: {source_orders}")
    # OBJ_ID_SN is a source/display order and can include ITEM's position.  A
    # table with ITEM at source position 1 may therefore expose its first real
    # dimension as OBJ_ID_SN=2.  Param API addresses only non-ITEM dimensions,
    # so assign contiguous objL1..objLn after sorting while preserving the raw
    # source order separately.
    for param_order, row in enumerate(ordered, 1):
        row["obj_order"] = param_order
    result_dimensions = [
        {key: value for key, value in row.items() if key != "_value_ids"}
        for row in ordered
    ]
    return items, result_dimensions


def build_live_profile(
    table_key: str,
    responses: Mapping[str, Any],
    *,
    response_sha256: Mapping[str, str],
    retrieved_at: str,
) -> dict[str, Any]:
    """Convert official metadata responses into a lossless cell-selector profile."""

    org_id, tbl_id = split_table_key(table_key)
    missing = [endpoint for endpoint in ENDPOINTS if endpoint not in responses]
    if missing:
        raise LiveMetadataError(f"metadata endpoint missing: {missing}")
    for endpoint in ENDPOINTS:
        if not isinstance(responses[endpoint], list):
            raise LiveMetadataError(f"{endpoint} response must be a list")

    items, dimensions = _ordered_inventory(list(responses["ITM"]))
    periods: list[dict[str, str]] = []
    seen_periods: set[tuple[str, str, str]] = set()
    for index, row in enumerate(responses["PRD"]):
        if not isinstance(row, Mapping):
            raise LiveMetadataError(f"PRD row {index} is not an object")
        period = (
            str(row.get("PRD_SE") or "").strip(),
            str(row.get("STRT_PRD_DE") or "").strip(),
            str(row.get("END_PRD_DE") or "").strip(),
        )
        if not all(period):
            raise LiveMetadataError(f"PRD row {index} misses frequency or range")
        if period not in seen_periods:
            seen_periods.add(period)
            periods.append(
                {"PRD_SE": period[0], "STRT_PRD_DE": period[1], "END_PRD_DE": period[2]}
            )
    if not periods:
        raise LiveMetadataError("PRD endpoint returned no periods")

    table_rows = responses["TBL"]
    table_name = ""
    if table_rows:
        first = table_rows[0]
        if not isinstance(first, Mapping):
            raise LiveMetadataError("TBL row is not an object")
        table_name = str(first.get("TBL_NM") or "").strip()

    unit_sources = [*items, *(value for dimension in dimensions for value in dimension["values"])]
    units = sorted({source["unit_nm"] for source in unit_sources if source["unit_nm"]})
    content = {
        "table_key": table_key,
        "org_id": org_id,
        "tbl_id": tbl_id,
        "tbl_name": table_name,
        "items": items,
        "dimensions": dimensions,
        "periods": periods,
        "units": units,
        "source": "KOSIS_METADATA_API",
        "source_endpoints": list(ENDPOINTS),
        "response_sha256": {endpoint: response_sha256[endpoint] for endpoint in ENDPOINTS},
        "retrieved_at": retrieved_at,
        "metadata_readiness": {
            "cell_selector_ready": True,
            "item_count": len(items),
            "dimension_count": len(dimensions),
            "dimension_value_count": sum(len(row["values"]) for row in dimensions),
            "period_count": len(periods),
            "items_with_unit": sum(bool(row["unit_nm"]) for row in items),
            "dimension_values_with_unit": sum(
                bool(value["unit_nm"])
                for dimension in dimensions
                for value in dimension["values"]
            ),
        },
    }
    content["profile_sha256"] = _sha256_bytes(
        _canonical_bytes({key: value for key, value in content.items() if key not in {"retrieved_at", "profile_sha256"}})
    )
    return content


def _load_checkpoint(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise LiveMetadataError(f"checkpoint row {number} is not an object")
        key = (str(row.get("table_key") or ""), str(row.get("endpoint") or ""))
        if not all(key) or key in result:
            raise LiveMetadataError(f"checkpoint identity invalid or duplicate: {key}")
        result[key] = row
    return result


def _append_checkpoint(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.exists() or temporary.exists():
        raise FileExistsError(f"refusing to overwrite output: {path}")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def collect_live_profiles(
    table_keys: Iterable[str],
    output_root: str | Path,
    api: Any,
    *,
    delay_seconds: float = 0.2,
    now: Callable[[], str] = _utc_now,
    before_endpoint: Callable[[str, str], Any] | None = None,
    after_endpoint: Callable[[Any, BaseException | None], None] | None = None,
) -> dict[str, Any]:
    """Fetch TBL/ITM/PRD once per unique table with resumable raw checkpoints."""

    keys = sorted({str(value) for value in table_keys})
    if not keys:
        raise LiveMetadataError("at least one table_key is required")
    for table_key in keys:
        split_table_key(table_key)
    output_root = Path(output_root)
    profiles_path = output_root / "profiles.jsonl"
    manifest_path = output_root / "manifest.json"
    if profiles_path.exists() or manifest_path.exists():
        raise FileExistsError(f"refusing to overwrite finalized metadata output: {output_root}")

    raw_dir = output_root / "raw_responses"
    checkpoint_path = raw_dir / "checkpoint.jsonl"
    checkpoint = _load_checkpoint(checkpoint_path)
    recorder = _ensure_recording_session(api)
    logical_calls = 0
    resumed = 0
    errors: list[dict[str, str]] = []

    for table_key in keys:
        org_id, tbl_id = split_table_key(table_key)
        for endpoint in ENDPOINTS:
            identity = (table_key, endpoint)
            raw_path = raw_dir / f"{_safe_table_key(table_key)}__{endpoint}.bin"
            if identity in checkpoint:
                row = checkpoint[identity]
                if not raw_path.exists() or _sha256_file(raw_path) != row.get("raw_sha256"):
                    raise LiveMetadataError(f"checkpoint raw mismatch: {table_key} {endpoint}")
                resumed += 1
                continue
            if raw_path.exists():
                raise LiveMetadataError(f"orphan raw response: {raw_path}")

            before = recorder.http_attempts if recorder is not None else 0
            reservation = before_endpoint(table_key, endpoint) if before_endpoint is not None else None
            try:
                response = api.get_meta(org_id, tbl_id, endpoint)
                if not isinstance(response, list):
                    raise LiveMetadataError(f"{endpoint} response must be a list")
                captured = recorder.last_response_bytes if recorder is not None else None
                raw = captured if isinstance(captured, bytes) else _canonical_bytes(response)
                status = "OK"
                error_type = error_message = ""
                if after_endpoint is not None:
                    after_endpoint(reservation, None)
            except Exception as error:
                if after_endpoint is not None:
                    after_endpoint(reservation, error)
                captured = recorder.last_response_bytes if recorder is not None else None
                raw = captured if isinstance(captured, bytes) else b""
                status = "ERROR"
                error_type = type(error).__name__
                error_message = safe_error_message(error, str(getattr(api, "api_key", "")))
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw)
            logical_calls += 1
            attempts = (recorder.http_attempts - before) if recorder is not None else 1
            row = {
                "table_key": table_key,
                "endpoint": endpoint,
                "status": status,
                "retrieved_at": now(),
                "raw_path": str(raw_path.relative_to(output_root)).replace("\\", "/"),
                "raw_sha256": _sha256_bytes(raw),
                "http_attempts": max(1, attempts),
            }
            if status == "ERROR":
                row.update({"error_type": error_type, "error_message": error_message})
                errors.append({"table_key": table_key, "endpoint": endpoint, "error_type": error_type, "error_message": error_message})
            _append_checkpoint(checkpoint_path, row)
            checkpoint[identity] = row
            if delay_seconds:
                time.sleep(delay_seconds)

    profiles: list[dict[str, Any]] = []
    profile_errors: list[dict[str, str]] = []
    for table_key in keys:
        responses: dict[str, Any] = {}
        shas: dict[str, str] = {}
        endpoint_failure = False
        for endpoint in ENDPOINTS:
            row = checkpoint[(table_key, endpoint)]
            if row["status"] != "OK":
                endpoint_failure = True
                continue
            raw_path = output_root / row["raw_path"]
            raw = raw_path.read_bytes()
            if _sha256_bytes(raw) != row["raw_sha256"]:
                raise LiveMetadataError(f"raw SHA drift: {table_key} {endpoint}")
            value = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(value, list):
                raise LiveMetadataError(f"stored response must be list: {table_key} {endpoint}")
            responses[endpoint] = value
            shas[endpoint] = row["raw_sha256"]
        if endpoint_failure:
            continue
        try:
            profiles.append(
                build_live_profile(
                    table_key,
                    responses,
                    response_sha256=shas,
                    retrieved_at=max(checkpoint[(table_key, endpoint)]["retrieved_at"] for endpoint in ENDPOINTS),
                )
            )
        except LiveMetadataError as error:
            profile_errors.append({"table_key": table_key, "error_type": type(error).__name__, "error_message": str(error)})

    profiles_bytes = b"".join(_canonical_bytes(row) + b"\n" for row in profiles)
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "source": "KOSIS_METADATA_API",
        "endpoints": list(ENDPOINTS),
        "table_count": len(keys),
        "ordered_table_keys": keys,
        "ordered_table_keys_sha256": _sha256_bytes(_canonical_bytes(keys)),
        "expected_logical_requests": len(keys) * len(ENDPOINTS),
        "logical_calls_this_run": logical_calls,
        "resumed_endpoint_count": resumed,
        "endpoint_error_count": len(errors),
        "profile_success_count": len(profiles),
        "profile_error_count": len(profile_errors),
        "profiles_sha256": _sha256_bytes(profiles_bytes),
        "checkpoint_sha256": _sha256_file(checkpoint_path),
        "code_sha256": _sha256_file(Path(__file__)),
        "errors": errors,
        "profile_errors": profile_errors,
    }
    _write_new(profiles_path, profiles_bytes)
    _write_new(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return manifest


def make_default_api(*, timeout: float = 20.0, retries: int = 0) -> KosisOpenAPI:
    api_key = get_api_key(None, Path.cwd() / ".env")
    return KosisOpenAPI(api_key, timeout=timeout, retries=retries, backoff_seconds=1.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table-keys", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    keys = json.loads(args.table_keys.read_text(encoding="utf-8"))
    if not isinstance(keys, list) or not all(isinstance(value, str) for value in keys):
        raise LiveMetadataError("table-keys file must be a JSON string list")
    report = collect_live_profiles(
        keys,
        args.output_root,
        make_default_api(timeout=args.timeout, retries=0),
        delay_seconds=args.delay_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTRACT_VERSION",
    "ENDPOINTS",
    "LiveMetadataError",
    "build_live_profile",
    "collect_live_profiles",
    "make_default_api",
    "split_table_key",
]




