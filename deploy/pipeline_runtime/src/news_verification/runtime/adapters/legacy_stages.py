"""Explicit seam for legacy L2/body-trace runners pending Phase 3."""

from __future__ import annotations

from typing import Any


def l2_runner() -> Any:
    from src.develop.run_l2_segmentation import run

    return run


def body_trace_api() -> Any:
    import importlib

    return importlib.import_module("src.develop.run_article_body_pipeline_trace_v1")


__all__ = ["body_trace_api", "l2_runner"]
