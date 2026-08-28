from __future__ import annotations

import sys as _sys

from src.news_verification.runtime import run_layer_stack as _implementation

_sys.modules[__name__] = _implementation

