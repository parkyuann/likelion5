"""Own only the local services started for one terminal pipeline run.

The launcher deliberately points Qdrant at the existing original storage.  It
does not copy, migrate, rebuild, or mutate the collection definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
from urllib import request

from src.news_verification.runtime.services.child_environment import strict_child_environment


SERVICE_BLOCKERS = frozenset({
    "QUERY_ENCODER_UNAVAILABLE",
    "RERANKER_UNAVAILABLE",
    "V6_QDRANT_UNAVAILABLE",
})

QDRANT_BINARY = Path("scratchpad_operational_eval_20260822/qdrant_bin/windows/qdrant.exe")
QDRANT_CONFIG = Path(
    "data/develop/pipeline_body_input_diagnostic_20260823_user_example_v1/"
    "trace_v1_services_20260823/qdrant_original_storage.yaml"
)
ENCODER_SNAPSHOT = Path(
    "data/develop/local_model_snapshots_20260823/"
    "dragonkue_BGE-m3-ko_7074d66aa46562342193ca4feb3d89bf9dad71b4"
)
RERANKER_SNAPSHOT = Path(
    "data/develop/local_model_snapshots_20260823/"
    "dragonkue_bge-reranker-v2-m3-ko_2aca5884ecac490192af9ebd86836d9073d826cd"
)


class LocalServiceStartError(RuntimeError):
    pass


@dataclass
class _OwnedProcess:
    name: str
    process: subprocess.Popen[Any]
    stdout: Any
    stderr: Any


@dataclass
class LocalPipelineServices:
    """Start missing loopback services and stop only processes owned here."""

    repo_root: Path
    config_path: Path
    log_root: Path
    python_executable: Path = field(default_factory=lambda: Path(sys.executable))
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen
    probe_fn: Callable[[str], bool] | None = None
    sleep_fn: Callable[[float], None] = time.sleep
    owned: list[_OwnedProcess] = field(default_factory=list, init=False)

    def _config(self) -> Mapping[str, Any]:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LocalServiceStartError("LOCAL_SERVICE_CONFIG_INVALID") from exc

    def _assert_assets(self) -> None:
        required = (
            self.repo_root / QDRANT_BINARY,
            self.repo_root / QDRANT_CONFIG,
            self.repo_root / ENCODER_SNAPSHOT,
            self.repo_root / RERANKER_SNAPSHOT,
            self.python_executable,
        )
        if any(not path.exists() for path in required):
            raise LocalServiceStartError("LOCAL_SERVICE_ASSET_MISSING")

    def _specs(self) -> dict[str, tuple[list[str], dict[str, str]]]:
        self._assert_assets()
        config = self._config()
        services = config.get("services") if isinstance(config.get("services"), Mapping) else {}
        expected = {
            "qdrant": "http://127.0.0.1:6335",
            "query_encoder": "http://127.0.0.1:8820",
            "reranker": "http://127.0.0.1:8819",
        }
        if any(str(services.get(name) or "").rstrip("/") != url for name, url in expected.items()):
            raise LocalServiceStartError("LOCAL_SERVICE_ENDPOINT_UNSUPPORTED")

        env = strict_child_environment(os.environ)
        env.update({
            "LOCAL_ENCODER_SNAPSHOT": str((self.repo_root / ENCODER_SNAPSHOT).resolve()),
            "LOCAL_RERANKER_SNAPSHOT": str((self.repo_root / RERANKER_SNAPSHOT).resolve()),
            "LOCAL_ENCODER_MODEL_REVISION": "7074d66aa46562342193ca4feb3d89bf9dad71b4",
            "LOCAL_RERANKER_MODEL_REVISION": "2aca5884ecac490192af9ebd86836d9073d826cd",
        })
        python = str(self.python_executable.resolve())
        return {
            "V6_QDRANT_UNAVAILABLE": (
                [str((self.repo_root / QDRANT_BINARY).resolve()), "--config-path", str((self.repo_root / QDRANT_CONFIG).resolve()), "--disable-telemetry"],
                env,
            ),
            "QUERY_ENCODER_UNAVAILABLE": (
                [python, "-m", "uvicorn", "src.news_verification.runtime.bge_m3_ko_query_encoder_service:create_app", "--factory", "--host", "127.0.0.1", "--port", "8820"],
                env,
            ),
            "RERANKER_UNAVAILABLE": (
                [python, "-m", "uvicorn", "src.news_verification.runtime.bge_reranker_v2_service:create_app", "--factory", "--host", "127.0.0.1", "--port", "8819"],
                env,
            ),
        }

    def start_missing(self, blockers: list[str], output_fn: Callable[[str], None] = print) -> None:
        missing = [name for name in blockers if name in SERVICE_BLOCKERS]
        if not missing:
            return
        specs = self._specs()
        # A live-stage recheck can restart only a service that died after the
        # initial readiness check.  Reuse this run's log directory and append
        # to its per-service logs; a new terminal run still gets a fresh path.
        self.log_root.mkdir(parents=True, exist_ok=True)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        labels = {
            "V6_QDRANT_UNAVAILABLE": "원본 Qdrant",
            "QUERY_ENCODER_UNAVAILABLE": "질의 encoder",
            "RERANKER_UNAVAILABLE": "reranker",
        }
        for blocker in missing:
            label = labels[blocker]
            stdout = (self.log_root / f"{label}.stdout.log").open("ab")
            stderr = (self.log_root / f"{label}.stderr.log").open("ab")
            argv, env = specs[blocker]
            try:
                process = self.popen_factory(
                    argv, cwd=str(self.repo_root), env=env, stdout=stdout, stderr=stderr,
                    creationflags=creationflags,
                )
            except Exception:
                stdout.close()
                stderr.close()
                raise
            self.owned.append(_OwnedProcess(label, process, stdout, stderr))
            output_fn(f"[SERVICE] {label} 자동 기동을 시작했습니다.")

    def wait_until_ready(
        self, *, timeout_seconds: float = 300.0, output_fn: Callable[[str], None] = print,
    ) -> None:
        if not self.owned:
            return
        config = self._config()
        services = config["services"]
        health_urls = {
            "원본 Qdrant": str(services["qdrant"]).rstrip("/") + "/healthz",
            "질의 encoder": str(services["query_encoder"]).rstrip("/") + "/health",
            "reranker": str(services["reranker"]).rstrip("/") + "/health",
        }
        probe = self.probe_fn or _probe
        deadline = time.monotonic() + timeout_seconds
        announced_at = 0.0
        while time.monotonic() < deadline:
            exited = [item.name for item in self.owned if item.process.poll() is not None]
            if exited:
                raise LocalServiceStartError("LOCAL_SERVICE_PROCESS_EXITED:" + ",".join(exited))
            waiting = [item.name for item in self.owned if not probe(health_urls[item.name])]
            if not waiting:
                output_fn("[SERVICE] 자동 기동한 로컬 서비스가 모두 준비됐습니다.")
                return
            now = time.monotonic()
            if now >= announced_at:
                output_fn("[SERVICE] 준비 대기 중: " + ", ".join(waiting))
                announced_at = now + 15.0
            self.sleep_fn(2.0)
        raise LocalServiceStartError("LOCAL_SERVICE_START_TIMEOUT")

    def stop_owned(self, output_fn: Callable[[str], None] = print) -> None:
        if not self.owned:
            return
        for item in reversed(self.owned):
            if item.process.poll() is None:
                item.process.terminate()
        for item in reversed(self.owned):
            try:
                item.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                item.process.kill()
                item.process.wait(timeout=10)
            finally:
                item.stdout.close()
                item.stderr.close()
        output_fn("[SERVICE] 이번 실행에서 기동한 로컬 서비스만 종료했습니다.")
        self.owned.clear()


def _probe(url: str) -> bool:
    try:
        with request.urlopen(url, timeout=2.0) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


__all__ = ["LocalPipelineServices", "LocalServiceStartError", "SERVICE_BLOCKERS"]

