"""Final-tree secret scan and sibling receipt finalization contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import os
import re
from typing import Iterable, Mapping
import uuid


# Match suspicious field names only at key-token boundaries.  Harmless
# contract fields such as ``secrets_persisted: false`` are not evidence.
# Deliberately accepts JSON, YAML-ish, dotenv, Markdown, and log forms.  The
# boundary prevents harmless names such as ``secrets_persisted`` and
# ``token_count`` from becoming secret evidence.
KEY_NAME_RE = re.compile(rb'(?i)(?<![A-Za-z0-9_-])["\']?(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|secret|token|password)["\']?(?![A-Za-z0-9_-])\s*(?::|=)', re.ASCII)
URL_CREDENTIAL_RE = re.compile(rb"(?i)https?://[^/\s:@]+:[^/@\s]+@")


class SecretScanError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanFile:
    path: str
    sha256: str
    bytes: int


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key_value_hits(data: bytes) -> int:
    """Count suspicious key/value pairs, ignoring false boolean declarations."""
    hits = 0
    # Consume one scalar or quoted value.  A credential is never included in
    # the exception text or receipt; only a count is returned to callers.
    scalar = re.compile(rb'(?is)^\s*("[^"]*"|\'[^\']*\'|[^\s,;\]}#]+)', re.ASCII)
    for match in KEY_NAME_RE.finditer(data):
        value_match = scalar.match(data[match.end():])
        if value_match is None:
            continue
        value = value_match.group(1).strip().strip(b'"\'').lower()
        if value in {b"", b"false", b"null", b"[]", b"{}"}:
            continue
        hits += 1
    return hits


def secret_env_manifest(names: Iterable[str], environ: Mapping[str, str] | None = None) -> list[dict[str, object]]:
    env = os.environ if environ is None else environ
    rows = []
    for name in sorted({str(n) for n in names}):
        value = str(env.get(name) or "")
        rows.append({"name": name, "present": bool(value), "sha256": _sha(value.encode()) if value else None})
    return rows


class FinalTreeSecretScanner:
    """Scan immutable bytes, then issue one atomic receipt beside the output."""

    def __init__(self, output_root: str | Path, *, receipt_path: str | Path | None = None, secrets: Iterable[str] = ()) -> None:
        self.output_root = Path(output_root).resolve()
        self.receipt_path = Path(receipt_path).resolve() if receipt_path else self.output_root.parent / f"{self.output_root.name}.secret_scan_receipt.json"
        if self.receipt_path.parent == self.output_root:
            raise SecretScanError("RECEIPT_MUST_BE_SIBLING")
        self.secrets = tuple(sorted({str(s) for s in secrets if str(s)}))
        self._finalized = False
        self._tree_hash = ""

    def _snapshot(self) -> tuple[list[ScanFile], list[tuple[Path, bytes]]]:
        if not self.output_root.is_dir():
            raise SecretScanError("OUTPUT_ROOT_MISSING")
        rows: list[ScanFile] = []
        payloads: list[tuple[Path, bytes]] = []
        for path in sorted((p for p in self.output_root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(self.output_root).as_posix()):
            data = path.read_bytes()
            rel = path.relative_to(self.output_root).as_posix()
            rows.append(ScanFile(rel, _sha(data), len(data)))
            payloads.append((path, data))
        tree_bytes = "".join(f"{row.path}\0{row.sha256}\n" for row in rows).encode()
        self._tree_hash = _sha(tree_bytes)
        return rows, payloads

    def scan(self) -> dict[str, object]:
        rows, payloads = self._snapshot()
        secret_hits = 0
        key_hits = 0
        url_hits = 0
        for _, data in payloads:
            secret_hits += sum(data.count(value.encode()) for value in self.secrets)
            key_hits += _key_value_hits(data)
            url_hits += len(URL_CREDENTIAL_RE.findall(data))
        if secret_hits or key_hits or url_hits:
            raise SecretScanError(f"SECRET_SCAN_FAILED:secret={secret_hits}:key={key_hits}:url={url_hits}")
        return {
            "contract": "audit-final-tree-secret-scan-v1",
            "files": [row.__dict__ for row in rows],
            "file_count": len(rows),
            "bytes": sum(row.bytes for row in rows),
            "secret_value_count": secret_hits,
            "pattern_hits": {"key_name": key_hits, "url_credentials": url_hits},
            "final_tree_sha256": self._tree_hash,
            "scanner_code_sha256": _sha(Path(__file__).read_bytes()),
        }

    def finalize(self) -> dict[str, object]:
        if self._finalized:
            raise SecretScanError("FINAL_TREE_ALREADY_FINALIZED")
        receipt = self.scan()
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt_path.with_name(self.receipt_path.name + f".{uuid.uuid4().hex}.tmp")
        receipt_bytes = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        # A receipt is itself scanned before it is published.  It contains no
        # secret values and cannot reference key/value evidence from the tree.
        # The attestation schema necessarily names its counters
        # ``secret_value_count`` and ``pattern_hits``; key-name scanning is
        # applied to the output tree, while receipt content is checked for
        # actual values and URL credentials.
        if any(value.encode() in receipt_bytes for value in self.secrets) or URL_CREDENTIAL_RE.search(receipt_bytes):
            raise SecretScanError("SECRET_SCAN_FAILED:receipt")
        # Keep the receipt hash external to the receipt bytes.  Embedding a
        # hash of the object that contains that hash is self-referential.
        receipt_sha256 = _sha(receipt_bytes)
        temporary.write_bytes(receipt_bytes)
        if self.receipt_path.exists():
            temporary.unlink(missing_ok=True)
            raise FileExistsError(self.receipt_path)
        os.replace(temporary, self.receipt_path)
        self._finalized = True
        receipt["receipt_sha256"] = receipt_sha256
        receipt["receipt_path"] = str(self.receipt_path)
        return receipt

    def assert_write_allowed(self) -> None:
        if self._finalized:
            raise SecretScanError("POST_SCAN_WRITE_FORBIDDEN")

    def write_after_scan(self, path: str | Path, data: bytes) -> None:
        """Guarded writer used by finalizers; no output bytes after receipt."""
        self.assert_write_allowed()
        target = Path(path).resolve()
        if self.output_root not in target.parents:
            raise SecretScanError("OUTPUT_WRITE_OUTSIDE_ROOT")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def verify_receipt(output_root: str | Path, receipt_path: str | Path, *, expected_receipt_sha256: str | None = None) -> dict[str, object]:
    """Independently re-scan the tree and validate the complete receipt."""
    root = Path(output_root).resolve()
    receipt_file = Path(receipt_path).resolve()
    try:
        receipt_bytes = receipt_file.read_bytes()
    except OSError:
        return {"valid": False, "receipt_sha256": None, "expected_receipt_sha256": expected_receipt_sha256, "error": "RECEIPT_UNREADABLE"}
    actual_receipt_sha = _sha(receipt_bytes)
    base = {"valid": False, "receipt_sha256": actual_receipt_sha, "expected_receipt_sha256": expected_receipt_sha256}
    if expected_receipt_sha256 is not None and actual_receipt_sha != str(expected_receipt_sha256):
        return {**base, "error": "RECEIPT_SHA_MISMATCH"}
    try:
        payload = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**base, "error": "RECEIPT_SCHEMA_INVALID"}
    if not isinstance(payload, Mapping):
        return {**base, "error": "RECEIPT_SCHEMA_INVALID"}
    expected_keys = {
        "contract", "files", "file_count", "bytes", "secret_value_count",
        "pattern_hits", "final_tree_sha256", "scanner_code_sha256",
    }
    if set(payload) != expected_keys:
        return {**base, "error": "RECEIPT_SCHEMA_INVALID"}
    canonical = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if canonical != receipt_bytes:
        return {**base, "error": "RECEIPT_CANONICAL_BYTES_MISMATCH"}

    def nonnegative_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    files = payload.get("files")
    if not isinstance(files, list):
        return {**base, "error": "RECEIPT_FILES_INVALID"}
    for row in files:
        if (
            not isinstance(row, Mapping) or set(row) != {"path", "sha256", "bytes"}
            or not isinstance(row.get("path"), str) or not row["path"]
            or not isinstance(row.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
            or not nonnegative_int(row.get("bytes"))
        ):
            return {**base, "error": "RECEIPT_FILES_INVALID"}
    pattern_hits = payload.get("pattern_hits")
    if (
        not isinstance(pattern_hits, Mapping)
        or set(pattern_hits) != {"key_name", "url_credentials"}
        or any(not nonnegative_int(pattern_hits.get(key)) for key in ("key_name", "url_credentials"))
    ):
        return {**base, "error": "RECEIPT_PATTERN_HITS_INVALID"}
    if any(pattern_hits[key] != 0 for key in ("key_name", "url_credentials")):
        return {**base, "error": "RECEIPT_PATTERN_HITS_NONZERO"}
    if not nonnegative_int(payload.get("secret_value_count")) or payload["secret_value_count"] != 0:
        return {**base, "error": "RECEIPT_SECRET_COUNT_INVALID"}
    if not nonnegative_int(payload.get("file_count")) or not nonnegative_int(payload.get("bytes")):
        return {**base, "error": "RECEIPT_ARITHMETIC_INVALID"}
    if payload["file_count"] != len(files) or payload["bytes"] != sum(int(row["bytes"]) for row in files):
        return {**base, "error": "RECEIPT_ARITHMETIC_INVALID"}
    if payload.get("contract") != "audit-final-tree-secret-scan-v1":
        return {**base, "error": "RECEIPT_CONTRACT_INVALID"}
    if not isinstance(payload.get("final_tree_sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", payload["final_tree_sha256"]) is None:
        return {**base, "error": "RECEIPT_TREE_SHA_INVALID"}
    if payload.get("scanner_code_sha256") != _sha(Path(__file__).read_bytes()):
        return {**base, "error": "SCANNER_CODE_SHA_MISMATCH"}

    try:
        fresh = FinalTreeSecretScanner(root).scan()
    except (OSError, SecretScanError):
        return {**base, "error": "FRESH_SCAN_FAILED"}
    valid = dict(payload) == fresh
    return {
        **base, "valid": valid,
        "error": None if valid else "FRESH_SCAN_MISMATCH",
        "final_tree_sha256": fresh.get("final_tree_sha256"),
        "receipt_final_tree_sha256": payload.get("final_tree_sha256"),
        "file_count": fresh.get("file_count"),
    }


__all__ = ["FinalTreeSecretScanner", "KEY_NAME_RE", "SecretScanError", "URL_CREDENTIAL_RE", "secret_env_manifest", "verify_receipt"]
