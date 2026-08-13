"""Value semantics for every number in the analysis domain - PRINCIPAL-OWNED.

The single most important modelling decision in this codebase
(docs/planning/05-calculation-methodology.md §2): a missing value is NEVER
silently treated as zero. Every quantity carries its provenance, and results
computed from assumed or missing inputs say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Provenance(StrEnum):
    SUPPLIER = "supplier"  # stated on the quote
    USER_INPUT = "user_input"  # typed by a human
    USER_ASSUMPTION = "user_assumption"  # a scenario assumption (e.g. quality risk 2%)
    CALCULATED = "calculated"
    DEFAULT = "default"  # system default, always disclosed
    MISSING = "missing"  # not stated; NOT zero


class Completeness(StrEnum):
    COMPLETE = "complete"
    ASSUMPTION_DEPENDENT = "assumption_dependent"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class Quantified:
    """A Decimal with provenance. value None <=> provenance MISSING."""

    value: Decimal | None
    provenance: Provenance
    note: str | None = None

    def __post_init__(self) -> None:
        if (self.value is None) != (self.provenance is Provenance.MISSING):
            raise ValueError(
                "Quantified: value is None iff provenance is MISSING "
                f"(got value={self.value!r}, provenance={self.provenance})"
            )

    @property
    def is_missing(self) -> bool:
        return self.provenance is Provenance.MISSING

    @property
    def is_assumed(self) -> bool:
        return self.provenance in (Provenance.USER_ASSUMPTION, Provenance.DEFAULT)

    @staticmethod
    def missing(note: str | None = None) -> Quantified:
        return Quantified(None, Provenance.MISSING, note)

    @staticmethod
    def supplier(value: Decimal, note: str | None = None) -> Quantified:
        return Quantified(value, Provenance.SUPPLIER, note)

    @staticmethod
    def assumption(value: Decimal, note: str | None = None) -> Quantified:
        return Quantified(value, Provenance.USER_ASSUMPTION, note)
