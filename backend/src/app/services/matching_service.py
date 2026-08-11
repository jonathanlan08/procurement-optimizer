"""Matching service — part matching for quote lines
(docs/planning/04-document-pipeline.md §10, docs/planning/03-api-contract.md
§4.12, app/domain/matching/matcher.py, app/models/documents.py's FROZEN
`PartMatchCandidate`/`MatchStrategy`).

Services never commit: the request-scoped `get_db` dependency
(app/api/deps.py) commits on success and rolls back on any raised exception.

## Operates on quote lines, post-materialization — not extraction fields

This task's own OBJECTIVE is explicit that matching runs against
`QuoteLine` rows (via `matched_rfq_line_id`/`part_id`), not
`extraction_fields` pre-materialization, documenting this as "matching
operates on materialized quotes in v0.1." A manually-entered `Quote`
(`source = 'manual'`) has real `QuoteLine` rows with no extraction run
behind them at all, and those are just as matchable — so "materialized"
here means "a real `Quote`/`QuoteLine` exists," not "was extracted."

## `generate_for_quote`, not `generate_for_run`

This task's own OBJECTIVE names the generation method `generate_for_run
(run_id)`. That does not fit this codebase for two independent reasons: (1)
`docs/planning/03-api-contract.md` §4.12's own literal route table — which
this task also cites as authoritative for `api/v1/matching.py` — addresses
generation by quote (`POST /quotes/{id}/match`), never by extraction run;
(2) a manually-entered quote (see above) has no `ExtractionRun` at all to
key on, yet its lines must be matchable too. Read together with the task's
own clarifying prose ("matching operates on materialized quotes"), `run` in
the task's own method name is treated as loose shorthand for "one execution
of the matching process," not literally `ExtractionRun.id` — this service
names the method `generate_for_quote(quote_id, ...)` to match both the
contract's addressing and the OBJECTIVE's own clarifying sentence, rather
than the task's literal (and, on this reading, self-contradicting) method
name.

## Catalog scope: the quote's own RFQ lines, not the full org catalog

This task's OBJECTIVE says "loads org part catalog + alternatives." Taken
completely literally (every `Part` row in the organization) that is
incompatible with the FROZEN schema: `PartMatchCandidate.rfq_line_id` is a
required (`NOT NULL`) composite FK, so a candidate cannot be persisted for
any part that is not targeted by *some* line of the quote's own RFQ.
04-document-pipeline.md §10 independently confirms this scope for the fuzzy
strategy specifically ("candidate set restricted to the RFQ's parts");
`_build_catalog` below applies that same restriction to all five strategies,
for the structural reason above, not merely the fuzzy-specific one the doc
states.

The catalog built for one quote is exactly: (a) every part any line of the
quote's RFQ requests (the only parts a persisted candidate can ever attach
to, via that RFQ line's id), plus (b) every part that is an *approved*
`part_alternatives` entry for one of those RFQ-line parts, even if that
alternative part is not itself requested anywhere on the RFQ — needed only
so `app.domain.matching.generate_candidates`'s `alternative` strategy can
recognize a quoted alternative's identity and re-target the match at its
*canonical* (RFQ-line) part, per §10's own rule. A part in group (b) that is
not also in group (a) has no `rfq_line_id` of its own; if strategies 1/2/4
also happen to match it directly (not via the `alternative` re-targeting),
that candidate is silently dropped rather than persisted with no RFQ line to
attach to — an accepted, documented gap for v0.1, not a silent bug.
`conditional`/`rejected` alternatives are excluded entirely (see
`app/domain/matching/matcher.py`'s own module docstring for why the
`alternative` strategy is approved-only here).

Duplicate-part RFQ lines (the same `part_id` requested on two different RFQ
lines of the same RFQ) are a real but rare edge case this v0.1 build does
not fully solve: `_build_catalog` maps each part to the *first* RFQ line
that requests it, so a persisted candidate's `rfq_line_id` may not be the
"right" one of several duplicates. Flagged here rather than silently
accepted.

## Auto-confirmation (strategy `internal_pn` only)

Per §10 ("only strategy 1 auto-confirms... everything else requires human
confirmation"): a generated `internal_pn` candidate is persisted with
`human_confirmed=True`/`is_selected=True`/`confirmed_at` set immediately (no
human action), and the owning `QuoteLine` is updated to
`match_status=AUTO` (not `CONFIRMED` — that status is reserved for a line a
human explicitly confirmed through `confirm_line_match`, giving the two a
real behavioral distinction rather than collapsing them). Every other
candidate is persisted unconfirmed and ranked for human review.

## Regeneration is sticky, keyed off `QuoteLine.match_status`

`generate_for_quote` only (re)generates candidates for lines still
`UNMATCHED` — an `AUTO` line already carries a `human_confirmed=True`
strategy-1 candidate (from a prior generation) that
`MatchingRepository.delete_unconfirmed_for_line` would never delete, so
re-running the matcher against it would attempt to insert a second row for
the same `(quote_line_id, part_id, strategy)` and violate
`uq_part_match_candidates_org_line_part_strategy`; skipping non-`UNMATCHED`
lines entirely avoids that and matches §10's "a confirmed match is sticky"
rule at the line level, not just the candidate level. `CONFIRMED` and
`REJECTED` lines are likewise never touched by regeneration.

## `POST /quote-lines/{id}/match` / `DELETE /quote-lines/{id}/match`

Per §4.12's literal request shape (`{rfq_line_id, confirmed:true, reason?}`
→ sets `matched_rfq_line_id`, `match_status=confirmed`), confirmation is
addressed by *quote line*, not by candidate id, and supplies the target RFQ
line directly rather than a `PartMatchCandidate` id. `confirm_line_match`
resolves the RFQ line's own `part_id` (every `RfqLine` targets exactly one
part — `app/models/rfqs.py`) and applies it to the `QuoteLine` directly; if
matching candidate row(s) already exist for that `(quote_line, part)` pair
they are marked confirmed/selected too (so a subsequent `GET .../matches`
reflects the human's choice), but a candidate row is not a precondition — a
reviewer may confirm a pairing the matcher never proposed. Any other
previously-confirmed candidate on the same line (a different part) is
un-confirmed as a side effect: a line has exactly one true match, and this
explicit confirmation supersedes whatever came before it. `unmatch_line`
(the `DELETE` route) is the inverse: clears the line back to `UNMATCHED` and
un-confirms whatever candidate had been confirmed for it. Neither route
requires an existing `PartMatchCandidate` id, so no `MatchStrategy` value is
ever invented for a purely-human pairing (the enum is FROZEN at exactly five
values, none of them "manual").
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Final

from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import ConflictStateError, NotFoundError
from app.core.ids import IdGenerator
from app.core.money import quantize
from app.domain.matching.matcher import CatalogPart, LineTexts, MatchConfig, generate_candidates
from app.models.documents import MatchStrategy, PartMatchCandidate
from app.models.quotes import QuoteLine, QuoteLineMatchStatus, QuotePriceBreak
from app.repositories.matching_repository import MatchingRepository, QuoteLineRepository
from app.repositories.part_repository import PartAlternativeRepository, PartRepository
from app.repositories.quote_repository import QuoteRepository
from app.repositories.rfq_repository import RfqRepository
from app.services.audit import AuditRecorder

_CONFIDENCE_SCALE: Final[Decimal] = Decimal("0.000001")

QuoteLineMatches = tuple[QuoteLine, list[PartMatchCandidate]]


def _line_audit_state(line: QuoteLine) -> dict[str, object]:
    return {
        "match_status": line.match_status.value,
        "matched_rfq_line_id": (
            str(line.matched_rfq_line_id) if line.matched_rfq_line_id is not None else None
        ),
        "part_id": str(line.part_id) if line.part_id is not None else None,
    }


class MatchingService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._match_repo = MatchingRepository(session, organization_id)
        self._line_repo = QuoteLineRepository(session, organization_id)
        self._quote_repo = QuoteRepository(session, organization_id)
        self._rfq_repo = RfqRepository(session, organization_id)
        self._part_repo = PartRepository(session, organization_id)
        self._alt_repo = PartAlternativeRepository(session, organization_id)

    # -- catalog construction ------------------------------------------

    def _build_catalog(
        self, rfq_id: uuid.UUID
    ) -> tuple[tuple[CatalogPart, ...], dict[uuid.UUID, uuid.UUID]]:
        """See module docstring "Catalog scope"."""
        rfq_lines = self._rfq_repo.list_lines(rfq_id)
        part_to_rfq_line: dict[uuid.UUID, uuid.UUID] = {}
        for line in rfq_lines:
            part_to_rfq_line.setdefault(line.part_id, line.id)

        canonical_ids = set(part_to_rfq_line.keys())
        parts_by_id = {}
        for part_id in canonical_ids:
            part = self._part_repo.get(part_id)
            if part is not None:
                parts_by_id[part_id] = part

        alt_of: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for canonical_id in canonical_ids:
            for alt in self._alt_repo.list_for_part(canonical_id, limit=200):
                if alt.approval_status != "approved" or alt.alternative_part_id is None:
                    continue
                alt_of[alt.alternative_part_id].append(canonical_id)
                if alt.alternative_part_id not in parts_by_id:
                    extra = self._part_repo.get(alt.alternative_part_id)
                    if extra is not None:
                        parts_by_id[alt.alternative_part_id] = extra

        catalog = tuple(
            CatalogPart(
                part_id=part.id,
                internal_part_number=part.internal_part_number,
                manufacturer_part_number=part.manufacturer_part_number,
                normalized_key=part.normalized_key,
                name=part.name,
                alternative_of=tuple(alt_of.get(part.id, ())),
            )
            for part in parts_by_id.values()
        )
        return catalog, part_to_rfq_line

    # -- generate --------------------------------------------------------

    def generate_for_quote(
        self, quote_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> list[QuoteLineMatches]:
        quote = self._quote_repo.get_or_raise(quote_id)
        lines = self._quote_repo.list_lines(quote_id)
        catalog, part_to_rfq_line = self._build_catalog(quote.rfq_id)
        config = MatchConfig()
        now = self._clock.now()

        total_candidates = 0
        auto_confirmed_lines = 0
        for line in lines:
            if line.match_status != QuoteLineMatchStatus.UNMATCHED:
                continue  # sticky — see module docstring
            line_texts = LineTexts(
                part_number_text=line.quoted_part_number or line.quoted_mpn,
                description_text=line.description,
            )
            candidates = generate_candidates(line_texts, catalog, config)

            self._match_repo.delete_unconfirmed_for_line(line.id)
            self._db.flush()

            persisted: list[PartMatchCandidate] = []
            for rank, cand in enumerate(candidates, start=1):
                rfq_line_id = part_to_rfq_line.get(cand.part_id)
                if rfq_line_id is None:
                    # No RFQ line targets this part — cannot satisfy the
                    # required rfq_line_id FK; see module docstring.
                    continue
                auto = cand.strategy is MatchStrategy.INTERNAL_PN
                row = PartMatchCandidate(
                    id=self._ids.new_id(),
                    organization_id=self._organization_id,
                    quote_line_id=line.id,
                    rfq_line_id=rfq_line_id,
                    part_id=cand.part_id,
                    strategy=cand.strategy,
                    confidence=quantize(cand.confidence, _CONFIDENCE_SCALE),
                    explanation=cand.explanation,
                    rank=rank,
                    is_selected=auto,
                    human_confirmed=auto,
                    confirmed_by_id=(actor_id if auto else None),
                    confirmed_at=(now if auto else None),
                )
                self._match_repo.add(row)
                persisted.append(row)
            self._db.flush()
            total_candidates += len(persisted)

            auto_row = next((c for c in persisted if c.human_confirmed), None)
            if auto_row is not None:
                line.part_id = auto_row.part_id
                line.matched_rfq_line_id = auto_row.rfq_line_id
                line.match_status = QuoteLineMatchStatus.AUTO
                auto_confirmed_lines += 1
        self._db.flush()

        self._audit.record(
            "matching.candidates_generated",
            entity_type="quote",
            entity_id=quote.id,
            after_state={
                "quote_id": str(quote.id),
                "candidate_count": total_candidates,
                "auto_confirmed_lines": auto_confirmed_lines,
            },
            explanation=(
                f"Generated {total_candidates} part-match candidate(s) for quote "
                f"{quote.id} ({auto_confirmed_lines} line(s) auto-confirmed)."
            ),
        )
        return self.list_for_quote(quote_id)

    # -- reads -------------------------------------------------------------

    def list_for_quote(self, quote_id: uuid.UUID) -> list[QuoteLineMatches]:
        self._quote_repo.get_or_raise(quote_id)
        lines = self._quote_repo.list_lines(quote_id)
        candidates = self._match_repo.list_for_quote_lines([line.id for line in lines])
        by_line: dict[uuid.UUID, list[PartMatchCandidate]] = defaultdict(list)
        for cand in candidates:
            by_line[cand.quote_line_id].append(cand)
        return [(line, by_line.get(line.id, [])) for line in lines]

    def price_breaks_for_line(self, quote_line_id: uuid.UUID) -> list[QuotePriceBreak]:
        """Passthrough to `QuoteRepository.list_price_breaks`, so
        `api/v1/matching.py` can build a `QuoteLineResponse` (which needs a
        line's price breaks) without constructing its own `QuoteRepository`."""
        return self._quote_repo.list_price_breaks(quote_line_id)

    # -- confirm / unmatch ---------------------------------------------

    def confirm_line_match(
        self,
        quote_line_id: uuid.UUID,
        *,
        rfq_line_id: uuid.UUID,
        reason: str | None,
        actor_id: uuid.UUID,
    ) -> QuoteLine:
        line = self._line_repo.get_or_raise(quote_line_id)
        quote = self._quote_repo.get_or_raise(line.quote_id)
        rfq_line = self._rfq_repo.get_line(quote.rfq_id, rfq_line_id)
        if rfq_line is None:
            raise NotFoundError("RfqLine")

        before = _line_audit_state(line)
        now = self._clock.now()
        part_id = rfq_line.part_id
        for cand in self._match_repo.list_for_quote_line(line.id):
            if cand.part_id == part_id:
                cand.human_confirmed = True
                cand.is_selected = True
                cand.confirmed_by_id = actor_id
                cand.confirmed_at = now
            elif cand.human_confirmed:
                # A different part was previously confirmed for this line —
                # this explicit confirmation supersedes it (module docstring).
                cand.human_confirmed = False
                cand.is_selected = False
                cand.confirmed_by_id = None
                cand.confirmed_at = None
            else:
                cand.is_selected = False

        line.part_id = part_id
        line.matched_rfq_line_id = rfq_line.id
        line.match_status = QuoteLineMatchStatus.CONFIRMED
        self._db.flush()

        self._audit.record(
            "matching.confirmed",
            entity_type="quote_line",
            entity_id=line.id,
            before_state=before,
            after_state={**_line_audit_state(line), "reason": reason},
            explanation=(
                f"Quote line {line.id} confirmed matched to RFQ line {rfq_line.id} "
                f"(part {part_id})" + (f": {reason}" if reason else "")
            ),
        )
        return line

    def unmatch_line(
        self, quote_line_id: uuid.UUID, *, reason: str | None, actor_id: uuid.UUID
    ) -> QuoteLine:
        line = self._line_repo.get_or_raise(quote_line_id)
        if line.match_status == QuoteLineMatchStatus.UNMATCHED:
            raise ConflictStateError("This quote line is not currently matched.")

        before = _line_audit_state(line)
        for cand in self._match_repo.list_for_quote_line(line.id):
            if cand.human_confirmed:
                cand.human_confirmed = False
                cand.is_selected = False
                cand.confirmed_by_id = None
                cand.confirmed_at = None

        line.part_id = None
        line.matched_rfq_line_id = None
        line.match_status = QuoteLineMatchStatus.UNMATCHED
        self._db.flush()

        self._audit.record(
            "matching.unmatched",
            entity_type="quote_line",
            entity_id=line.id,
            before_state=before,
            after_state={**_line_audit_state(line), "reason": reason},
            explanation=f"Quote line {line.id} unmatched" + (f": {reason}" if reason else ""),
        )
        return line


__all__ = ["MatchingService", "QuoteLineMatches"]
