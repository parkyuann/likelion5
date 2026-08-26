"""KOSIS table-catalog adapter boundary.

The PostgreSQL metadata reader and OpenSearch adapter are owned by the search/data
team and are not included in this handoff.  Consequently this module contains no
local catalog fallback: callers fail closed until that contract is delivered.
"""

from __future__ import annotations

from typing import Any

from backend.runtime_gate import require_search_adapter


def kosis_table_url(org_id: str, tbl_id: str) -> str:
    return f"https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"


def search_tables(
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
    organization: str = "",
) -> dict[str, Any]:
    """Search is unavailable until the release-pinned adapter is supplied."""

    del query, limit, offset, organization
    require_search_adapter()
    raise AssertionError("unreachable")


def get_table(table_key: str) -> dict[str, Any] | None:
    """Table dereference is unavailable until the metadata contract is supplied."""

    del table_key
    require_search_adapter()
    raise AssertionError("unreachable")
