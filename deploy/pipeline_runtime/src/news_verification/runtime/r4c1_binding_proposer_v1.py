"""Gold-blind semantic surface proposals for R4-C1 late binding.

This module never selects an inventory ID.  It expands a tiny, versioned
Korean statistical terminology registry into claim spans and returns every
matching proposal.  The projection lattice and global validator retain sole
authority to reject span reuse and multiple full assignments.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


CONTRACT_VERSION = "r4c1-binding-proposer-v1"
TERMINOLOGY_REGISTRY_VERSION = 1


@dataclass(frozen=True)
class SurfaceProposal:
    start: int
    end: int
    text: str
    rule_id: str
    rule_version: int = 1


# Canonical KOSIS wording -> common claim wording.  Rules are terminology
# mappings, never table IDs, candidate ranks, article literals, or numbers.
_ALIASES = (
    ("국민총소득", "국민소득", "ko-stat-gni-common-name"),
    ("수납액", "세수", "ko-tax-receipts-common-name"),
    ("출생건수", "출생아 수", "ko-stat-birth-count-common-name"),
)


def _norm_map(surface: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(surface):
        if not re.match(r"[\s\-_./:(),]+", char):
            chars.append(char.casefold())
            positions.append(index)
    return "".join(chars), positions


def _submatches(surface: str, label: str) -> list[tuple[int, int, str]]:
    normalized, positions = _norm_map(surface)
    target, _ = _norm_map(label)
    if not normalized or not target:
        return []
    result: list[tuple[int, int, str]] = []
    for match in re.finditer(re.escape(target), normalized):
        start = positions[match.start()]
        end = positions[match.end() - 1] + 1
        result.append((start, end, surface[start:end]))
    return result


def _base_inventory_label(value: Any) -> str:
    # Parentheses in KOSIS labels commonly carry unit/vintage/footnote detail.
    # Removing them only creates proposals; the full label/ID and selected
    # series unit remain mandatory evidence downstream.
    return re.sub(r"\([^()]*\)", "", str(value or "")).strip()


def propose_semantic_alias_matches(
    claim_surface: Any,
    inventory_label: Any,
    *,
    allow_parenthetical_base: bool = False,
) -> tuple[SurfaceProposal, ...]:
    surface = str(claim_surface or "")
    inventory_text = str(inventory_label or "")
    base_label = _base_inventory_label(inventory_text)
    proposals: dict[tuple[int, int, str], SurfaceProposal] = {}
    # KOSIS frequently stores a measure as a dimension value whose label ends
    # in a unit qualifier (for example ``지표명(명)``).  Article wording often
    # contains only the base label.  This proposal consumes only the base
    # claim span; the full profile label and unit remain downstream evidence.
    if allow_parenthetical_base and base_label and base_label != inventory_text.strip():
        for start, end, text in _submatches(surface, base_label):
            proposal = SurfaceProposal(start, end, text, "parenthetical-qualifier-base")
            proposals[(start, end, proposal.rule_id)] = proposal
    for canonical, common, rule_id in _ALIASES:
        if canonical not in base_label:
            continue
        alias_label = base_label.replace(canonical, common)
        for start, end, text in _submatches(surface, alias_label):
            proposal = SurfaceProposal(start, end, text, rule_id)
            proposals[(start, end, rule_id)] = proposal
    return tuple(sorted(proposals.values(), key=lambda row: (row.start, row.end, row.rule_id)))


__all__ = [
    "CONTRACT_VERSION", "TERMINOLOGY_REGISTRY_VERSION", "SurfaceProposal",
    "propose_semantic_alias_matches",
]

