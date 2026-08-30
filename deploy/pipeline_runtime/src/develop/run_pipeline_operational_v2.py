from __future__ import annotations

import sys as _sys

from src.news_verification.runtime import run_pipeline_operational_v2 as _implementation

_sys.modules[__name__] = _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
