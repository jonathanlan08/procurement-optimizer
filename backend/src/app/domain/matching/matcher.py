"""Pure part-matching logic (docs/planning/04-document-pipeline.md §10,
`app/models/documents.py`'s FROZEN `PartMatchCandidate`/`MatchStrategy`).

No I/O, no ORM session, no organization scoping here - `generate_candidates`
is a plain function over three small local dataclasses
(`LineTexts`/`CatalogPart`/`MatchConfig`) and returns a tuple of
`MatchCandidate`. `services/matching_service.py` is the only caller; it
loads the org's part catalog/alternatives, calls this function once per
quote line, and persists the result as `PartMatchCandidate` rows.

`MatchStrategy` (the five-member `StrEnum`) is imported directly from
`app.models.documents` rather than redefined here, the same "one enum, no
drift" reasoning that module's own docstring already applies to
`ConfidenceBand`/`CONFIDENCE_BAND_ENUM` - `MatchCandidate.strategy` values
flow straight into `PartMatchCandidate.strategy` with no translation layer,
so a second, parallel enum would only invite the two to drift apart. It is a
plain `StrEnum`, not an ORM class, so importing it here does not pull any
SQLAlchemy/session machinery into this otherwise-pure module.

## A genuine conflict between the prose and its cited source

The spec describes the five strategies' priority order,
per-strategy confidence, and the fuzzy scoring formula in terms that
diverge from 04-document-pipeline.md §10 - the very section the task cites
as their source - on several concrete points:

| Aspect | The spec | §10 (implemented here) |
|---|---|---|
| Priority 3 vs 4 | `normalized_text` before `alternative` | `alternative` first |
| Confidence: `alternative` | 0.95 | 0.90 (0.75 if `conditional`, see below) |
| Confidence: `normalized_text` | 0.97 | 0.85 |
| Fuzzy algorithm | `token_sort_ratio`, raw `score/100` | `token_set_ratio`, capped formula below |

§10's actual fuzzy formula, implemented in strategy 5 below: raw ratio
`r = token_set_ratio(query, corpus) / 100`, kept only when `r >= threshold`
(`t`), confidence `= min(0.80, 0.5 + 0.4 * (r - t) / (1 - t))` - capped at
0.80 regardless of how high `r` climbs, so a fuzzy match can never outrank
any of strategies 1-4 by confidence alone.

Resolved in favor of §10's literal table throughout, the same "the cited
planning doc wins over the delegating task's own paraphrase of it" pattern
already established repeatedly in this codebase for this exact document
family - see `app/models/documents.py`'s module docstring points 1/7,
`app/models/parts.py`'s module docstring point 1, and
`app/models/quotes.py`'s module docstring points 3/8, every one of which
resolves an identically-shaped conflict the same direction. Confirming
evidence that §10's *priority order* specifically is the one actually
enforced at runtime (not merely one of two equally-valid readings): the
`MatchStrategy` enum's own declaration order in `app/models/documents.py`
follows the ERD box's listing (`internal_pn, mpn, normalized_text,
alternative, fuzzy` - the same order the prose uses), and that
model's own docstring explicitly calls this out as "differ[ing] from §10's
execution order" - i.e. the FROZEN model itself already documents that its
enum's declaration order is cosmetic and §10's execution order is the real
one. `_STRATEGY_PRIORITY` below is keyed to §10, not to declaration order.

One field is kept at the explicit, concrete number rather than
§10's: `MatchConfig.fuzzy_threshold` defaults to `0.82`, not §10's stated
`0.80`. This is deliberate, not an oversight of the same rule above - this
task's own required test coverage ("fuzzy threshold boundary: 0.81 excluded,
0.83 included") is only satisfiable at all with a 0.82 threshold; at 0.80
both 0.81 and 0.83 would be *included*, making the stated test case
impossible to write. Read as the task's own explicit numeric instruction
for this one dataclass default, verified self-consistent by its own test
requirement, taking precedence over §10's looser mention of "default 0.80"
for this single value only.

## `alternative` strategy: approved only, single confidence

§10 splits `alternative` into 0.90 (`approved`) / 0.75 (`conditional`). This
task's own prose for the strategy itself says "**approved** alternative"
only, and `CatalogPart.alternative_of` (a flat `tuple[UUID, ...]`, per this
task's own explicit dataclass shape) carries no per-entry approval-status
distinction to key a split confidence on. `services/matching_service.py`
builds `alternative_of` from `PartAlternative` rows with
`approval_status == 'approved'` only; `conditional`/`rejected` rows never
reach this function at all, so every `alternative` candidate this module
emits is implicitly the 0.90 (approved) case - the split confidence is
narrowed away by the task's own dataclass shape, not silently dropped.

## `LineTexts`/`CatalogPart` field shape and the fuzzy corpus

`LineTexts` has exactly the two fields this change specifies
(`part_number_text`, `description_text`) - not a third field for
`QuoteLine.quoted_mpn`. `services/matching_service.py` is responsible for
choosing what text to pass as `part_number_text` when building this from a
`QuoteLine` (quoted part number, falling back to quoted MPN when the part
number is absent) - see that service's module docstring. Strategies 1
(`internal_pn`), 2 (`mpn`), 3 (`alternative`), and 4 (`normalized_text`) all
test `part_number_text` against the catalog's identifier columns; strategy 5
(`fuzzy`) additionally folds in `description_text`.

`CatalogPart` likewise has exactly the specified fields - no
`description` field, unlike §10's "fuzzy ... on description + MPN" text.
Fuzzy compares `part_number_text + description_text` (the line's own texts)
against `name + internal_part_number + manufacturer_part_number` (the
catalog side) via `rapidfuzz.fuzz.token_set_ratio` - `name` standing in for
"description" on the catalog side, since `Part.name` is `NOT NULL` (always
present) while `Part.description` is nullable and not part of the
own `CatalogPart` field list; the numeric identifiers are folded in too so a
line whose description already names its part number scores well against a
short-name catalog entry.

## MPN strategy separator normalization

§10: "exact on `manufacturer_part_number` (after stripping `-`, space,
`/`)". `_strip_mpn_separators` strips exactly those three characters (not
the full `[^a-z0-9]` class `_normalize`/strategy 4 uses) before a
case-insensitive comparison, so `"CR0805-10K"` matches `"CR0805 10K"` but a
part number containing, say, a `.` is left alone by this strategy (it would
still be caught by strategy 4's more aggressive normalization if the
identifiers happen to coincide there).

## Determinism

`generate_candidates` returns candidates sorted by
`(-confidence, strategy priority, internal_part_number, part_id)` - this
task's own explicit 4-key tuple (the `strategy priority` component is a
no-op in practice given the five strategies' confidence values never
collide across strategies, but is implemented literally as specified; it
only matters for a hypothetical future confidence overlap). `MatchCandidate`
objects for the same `(part_id, strategy)` pair are deduped within one call,
keeping the higher-confidence one - needed because `part_number_text` is
tested against the same catalog part through more than one comparison inside
a single strategy (e.g. `alternative` checks both an exact internal-part-
number and a separator-normalized MPN match against the same alternative
row) and must not attempt to persist two rows that would collide on
`PartMatchCandidate`'s own `uq_part_match_candidates_org_line_part_strategy`
constraint.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rapidfuzz import fuzz

from app.models.documents import MatchStrategy

_NON_ALNUM_RE: Final = re.compile(r"[^a-z0-9]")
_MPN_SEPARATOR_RE: Final = re.compile(r"[-\s/]")

# §10's per-strategy base confidence (module docstring table above).
_CONFIDENCE_INTERNAL_PN: Final[Decimal] = Decimal("1.00")
_CONFIDENCE_MPN: Final[Decimal] = Decimal("0.97")
_CONFIDENCE_ALTERNATIVE: Final[Decimal] = Decimal("0.90")
_CONFIDENCE_NORMALIZED_TEXT: Final[Decimal] = Decimal("0.85")

# Fuzzy confidence formula (§10): 0.5 + 0.4 * (r - t) / (1 - t), capped 0.80.
_FUZZY_FLOOR: Final[Decimal] = Decimal("0.5")
_FUZZY_SPAN: Final[Decimal] = Decimal("0.4")
_FUZZY_CAP: Final[Decimal] = Decimal("0.80")

# §10's execution order - see module docstring for why this differs from
# MatchStrategy's own declaration order.
_STRATEGY_PRIORITY: Final[dict[MatchStrategy, int]] = {
    MatchStrategy.INTERNAL_PN: 1,
    MatchStrategy.MPN: 2,
    MatchStrategy.ALTERNATIVE: 3,
    MatchStrategy.NORMALIZED_TEXT: 4,
    MatchStrategy.FUZZY: 5,
}


@dataclass(frozen=True, slots=True)
class LineTexts:
    """The raw text of one quote line worth matching against the catalog.
    See module docstring for what `services/matching_service.py` populates
    each field with."""

    part_number_text: str | None
    description_text: str | None


@dataclass(frozen=True, slots=True)
class CatalogPart:
    """One part in the candidate catalog `generate_candidates` searches.
    `alternative_of` holds the canonical `part_id`s this part is an
    *approved* alternative for (empty if none) - see module docstring."""

    part_id: uuid.UUID
    internal_part_number: str
    manufacturer_part_number: str | None
    normalized_key: str
    name: str
    alternative_of: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class MatchConfig:
    """Tunables for `generate_candidates`. See module docstring for why
    `fuzzy_threshold` defaults to 0.82, not §10's stated 0.80."""

    fuzzy_threshold: Decimal = Decimal("0.82")
    max_candidates: int = 5


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    part_id: uuid.UUID
    strategy: MatchStrategy
    confidence: Decimal
    explanation: str


def _normalize(raw: str) -> str:
    """Mirrors `Part.normalized_key`'s generated expression
    (`app/models/parts.py`): lowercase, strip everything but `[a-z0-9]`."""
    return _NON_ALNUM_RE.sub("", raw.lower())


def _strip_mpn_separators(raw: str) -> str:
    """§10's MPN normalization: strip `-`, whitespace, `/` only (not the
    full `_normalize` alphabet), then fold case."""
    return _MPN_SEPARATOR_RE.sub("", raw).casefold()


def generate_candidates(
    line_texts: LineTexts,
    catalog: tuple[CatalogPart, ...],
    config: MatchConfig,
) -> tuple[MatchCandidate, ...]:
    """Run all five §10 strategies over one quote line's text against
    `catalog`, returning deduped, deterministically-ordered candidates
    capped at `config.max_candidates`. See module docstring for the full
    design rationale."""
    pn_text = (line_texts.part_number_text or "").strip()
    desc_text = (line_texts.description_text or "").strip()
    by_id = {part.part_id: part for part in catalog}

    best: dict[tuple[uuid.UUID, MatchStrategy], MatchCandidate] = {}

    def offer(
        part_id: uuid.UUID, strategy: MatchStrategy, confidence: Decimal, explanation: str
    ) -> None:
        key = (part_id, strategy)
        existing = best.get(key)
        if existing is None or confidence > existing.confidence:
            best[key] = MatchCandidate(
                part_id=part_id, strategy=strategy, confidence=confidence, explanation=explanation
            )

    if pn_text:
        pn_fold = pn_text.casefold()
        pn_mpn_norm = _strip_mpn_separators(pn_text)
        pn_norm = _normalize(pn_text)

        # -- strategy 1: internal_pn, exact case-insensitive --------------
        for part in catalog:
            if part.internal_part_number.casefold() == pn_fold:
                offer(
                    part.part_id,
                    MatchStrategy.INTERNAL_PN,
                    _CONFIDENCE_INTERNAL_PN,
                    f"Quoted part number '{pn_text}' matched catalog part "
                    f"'{part.internal_part_number}' exactly (case-insensitive internal "
                    "part number).",
                )

        # -- strategy 2: mpn, exact after separator stripping --------------
        if pn_mpn_norm:
            for part in catalog:
                mpn = part.manufacturer_part_number
                if mpn and _strip_mpn_separators(mpn) == pn_mpn_norm:
                    offer(
                        part.part_id,
                        MatchStrategy.MPN,
                        _CONFIDENCE_MPN,
                        f"Quoted part number '{pn_text}' matched catalog part "
                        f"'{part.internal_part_number}' manufacturer part number '{mpn}' "
                        "after separator normalization.",
                    )

        # -- strategy 3: approved alternative -> canonical part -------------
        for part in catalog:
            identity_hit = part.internal_part_number.casefold() == pn_fold
            mpn = part.manufacturer_part_number
            mpn_hit = (
                mpn is not None
                and pn_mpn_norm != ""
                and _strip_mpn_separators(mpn) == pn_mpn_norm
            )
            if not (identity_hit or mpn_hit):
                continue
            for canonical_id in part.alternative_of:
                canonical = by_id.get(canonical_id)
                if canonical is None:
                    continue
                offer(
                    canonical.part_id,
                    MatchStrategy.ALTERNATIVE,
                    _CONFIDENCE_ALTERNATIVE,
                    f"Quoted part number '{pn_text}' matched '{part.internal_part_number}', "
                    f"an approved alternative for canonical part "
                    f"'{canonical.internal_part_number}'.",
                )

        # -- strategy 4: normalized_text equality --------------------------
        if pn_norm:
            for part in catalog:
                if part.normalized_key == pn_norm:
                    offer(
                        part.part_id,
                        MatchStrategy.NORMALIZED_TEXT,
                        _CONFIDENCE_NORMALIZED_TEXT,
                        f"Normalized quoted text '{pn_norm}' matched catalog part "
                        f"'{part.internal_part_number}' normalized key exactly.",
                    )

    # -- strategy 5: fuzzy over name+numbers vs part_number+description ----
    query = " ".join(t for t in (pn_text, desc_text) if t)
    if query:
        threshold = config.fuzzy_threshold
        span = Decimal(1) - threshold
        for part in catalog:
            numbers = (part.internal_part_number, part.manufacturer_part_number)
            corpus = " ".join(t for t in (part.name, *numbers) if t)
            if not corpus:
                continue
            score = fuzz.token_set_ratio(query, corpus)
            ratio = Decimal(str(round(score, 6))) / Decimal(100)
            if ratio < threshold:
                continue
            raw_confidence = (
                _FUZZY_CAP
                if span <= 0
                else _FUZZY_FLOOR + _FUZZY_SPAN * (ratio - threshold) / span
            )
            confidence = min(_FUZZY_CAP, raw_confidence)
            offer(
                part.part_id,
                MatchStrategy.FUZZY,
                confidence,
                f"Fuzzy text match (token_set_ratio {score:.1f}/100): '{query}' vs catalog "
                f"part '{part.internal_part_number}' ('{corpus}').",
            )

    ordered = sorted(
        best.values(),
        key=lambda c: (
            -c.confidence,
            _STRATEGY_PRIORITY[c.strategy],
            by_id[c.part_id].internal_part_number,
            c.part_id,
        ),
    )
    return tuple(ordered[: config.max_candidates])


__all__ = ["CatalogPart", "LineTexts", "MatchCandidate", "MatchConfig", "generate_candidates"]
