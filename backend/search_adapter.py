"""Future search-team integration seam; no adapter implementation is shipped."""

from __future__ import annotations

from typing import Any, Protocol


class SearchAdapter(Protocol):
    def search(self, query: str, *, limit: int) -> list[dict[str, Any]]: ...


SEARCH_ADAPTER_STATUS = "SEARCH_ADAPTER_PENDING"
