"""`AiNarrativeProvider` Protocol - the boundary every negotiation-brief
narrative source implements (docs/SPEC.md §Negotiation brief,
app/core/config.py `NarrativeProviderKind`).

Mirrors `app.providers.extraction.base.ExtractionProvider`'s shape
(`name`/a boolean capability flag/one pure method), with one deliberate
naming difference: `is_generated`, not `is_simulated`. `ExtractionProvider.
is_simulated` answers "did this call a real external service" (a mock always
says yes-simulated). `is_generated` instead answers "is this section's TEXT
genuinely AI-authored prose, as opposed to a deterministic template filling
in blanks" - the distinction `app.models.briefs.NegotiationBrief.simulated`
is derived FROM (`simulated = not is_generated`; see that model's module
docstring). `TemplateNarrativeProvider.is_generated = False` always, by
construction - see that module.

**Trust boundary, mirroring `ExtractionProvider`'s (SPEC §Document and AI
security applied to §Negotiation brief's own "AI must not invent" list):**
`render_sections` receives ONLY `brief_facts` - a plain dict of strings
(pre-formatted decimal strings via `app.core.money.to_wire`, plain text, or
lists of either) that `app.services.brief_service` has already assembled
from stored, org-scoped data. A provider must never reach into the database,
call an external network service outside its own declared adapter, or
invent a number/name/date that is not already a value somewhere in
`brief_facts` - `brief_service`'s numeric cross-check enforces the "no
invented numbers" half of that mechanically after every call (see that
module's own docstring for what it does and does not catch).

Only `TemplateNarrativeProvider` (`template.py`) ships in this build. A real
`anthropic`-backed adapter is roadmap, exactly like `ExtractionProvider`'s
optional Anthropic adapter - `app.services.brief_service.
build_narrative_provider` raises `ProviderUnavailableError` for
`NarrativeProviderKind.ANTHROPIC` rather than silently substituting the
template provider (same precedent `extraction_service.
build_extraction_provider` sets).
"""

from __future__ import annotations

from typing import Any, Protocol

# The exact section keys `render_sections` must return, one deterministic
# sentence-or-more of prose each, built ONLY from `brief_facts` values.
# brief_service.py labels most of these `SectionProvenance.AI_NARRATIVE` (the
# synthesized/connective sections); `procurement_objective` is the one
# exception - its provenance is CALCULATED by default or USER_ASSUMPTION when
# the caller supplied an override, decided by brief_service, independent of
# having passed through this provider (see that module's own docstring). The
# remaining SPEC sections (quoted_unit_price, effective_unit_cost,
# price_target, stretch_target, walk_away_threshold, volume_leverage,
# payment_terms_opportunity) are written directly by brief_service as
# CALCULATED/SUPPLIER_PROVIDED/MISSING facts, never passed through a
# narrative provider at all - they are bare figures with a disclosed
# formula, not synthesized prose.
NARRATIVE_SECTION_KEYS: tuple[str, ...] = (
    "procurement_objective",
    "supplier_position",
    "lead_time_concerns",
    "quality_concerns",
    "spec_concerns",
    "alternative_suppliers",
    "batna",
    "recommended_concessions",
    "questions_for_clarification",
    "draft_email_subject",
    "draft_email_body",
)


class AiNarrativeProvider(Protocol):
    """Implementations: `TemplateNarrativeProvider` (deterministic, default)
    and the optional Anthropic adapter (env-gated, not implemented in this
    build)."""

    name: str
    is_generated: bool  # True only for a real AI-authored narrative

    def render_sections(self, brief_facts: dict[str, Any]) -> dict[str, str]:
        """Returns exactly `NARRATIVE_SECTION_KEYS`, mapped to rendered text.
        Must read `brief_facts` only - no database, no network, no clock,
        no randomness (the template provider is fully deterministic;
        the same facts must always render to the same text, forever within
        a provider version, mirroring `VendorScorer`'s reproducibility
        contract)."""
        ...


__all__ = ["NARRATIVE_SECTION_KEYS", "AiNarrativeProvider"]
