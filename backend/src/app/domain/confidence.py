"""Extraction-confidence bands - single source of truth ().

Mirrored by the design system (design-system/procurement-optimizer/MASTER.md);
this module is authoritative. Thresholds per 00-decisions.md §4 ruling #27.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

CONFIDENCE_HIGH: Final[Decimal] = Decimal("0.95")  # >= high: no review required
CONFIDENCE_LOW: Final[Decimal] = Decimal("0.60")  # < low: confirmation mandatory


class ConfidenceBand(StrEnum):
    HIGH = "high"  # >= 0.95 - accepted automatically, still correctable
    MEDIUM = "medium"  # [0.60, 0.95) - requires review
    LOW = "low"  # < 0.60 - must be confirmed before use


def band(confidence: Decimal) -> ConfidenceBand:
    if not Decimal(0) <= confidence <= Decimal(1):
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")
    if confidence >= CONFIDENCE_HIGH:
        return ConfidenceBand.HIGH
    if confidence >= CONFIDENCE_LOW:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW
