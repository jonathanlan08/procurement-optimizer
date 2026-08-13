"""`TemplateNarrativeProvider` — the only `AiNarrativeProvider` shipped in
v0.1 (`base.py`'s own docstring: the optional Anthropic adapter is
roadmap), mirroring `app.providers.extraction.mock.MockExtractionProvider`'s
role as "the deterministic default every environment can run without a paid
key" (SPEC §External-service strategy: "Public demo works without any paid
AI key").

Purely functional: no network I/O, no randomness, no mutable state, no
clock. Every sentence is built with plain `str.format`/f-strings, reading
`brief_facts` ONLY — never inventing a number, name, or date not already a
value somewhere in that dict (`app.services.brief_service`'s numeric
cross-check enforces this mechanically after every call; this module's own
discipline is the first line of defense, per SPEC §Negotiation brief's "AI
must not invent market benchmarks, competitor quotes, historical prices,
supplier behavior, savings, concessions, or delivery promises").

**Expected `brief_facts` keys** (all `str`, pre-formatted by
`brief_service.py` — `to_wire` decimal strings, or plain text; a `list[str]`
value is rendered here by joining with `"; "`, empty lists render as an
explicit "none recorded" phrase rather than being silently blank, per the
"missing is shown, never omitted" convention `app/domain/values.py`
establishes for the rest of this codebase). Every key below is always
present (never absent from the dict) though its VALUE may be a plain
"not stated"/"none"/"not available" string when the underlying data point is
missing — `render_sections` performs no null-handling of its own beyond
`.get(..., "not available")` defensive defaults, since `brief_service` is
the single place that decides what "missing" looks like as text.
"""

from __future__ import annotations

from typing import Any

PROVIDER_NAME = "template"


def _join(items: Any, *, empty: str) -> str:
    if isinstance(items, list):
        return "; ".join(str(i) for i in items) if items else empty
    return str(items) if items else empty


def _get(facts: dict[str, Any], key: str, default: str = "not available") -> str:
    value = facts.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, list):
        return _join(value, empty=default)
    return str(value)


class TemplateNarrativeProvider:
    """Deterministic template narrative: same `brief_facts` -> same text,
    forever within this module's version (no version constant needed — the
    module IS the version; a wording change is a code change, reviewed like
    any other)."""

    name = PROVIDER_NAME
    is_generated = False  # never AI-authored — see base.py module docstring

    def render_sections(self, brief_facts: dict[str, Any]) -> dict[str, str]:
        f = brief_facts
        return {
            "procurement_objective": self._procurement_objective(f),
            "supplier_position": self._supplier_position(f),
            "lead_time_concerns": self._lead_time_concerns(f),
            "quality_concerns": self._quality_concerns(f),
            "spec_concerns": self._spec_concerns(f),
            "alternative_suppliers": self._alternative_suppliers(f),
            "batna": self._batna(f),
            "recommended_concessions": self._recommended_concessions(f),
            "questions_for_clarification": self._questions_for_clarification(f),
            "draft_email_subject": self._email_subject(f),
            "draft_email_body": self._email_body(f),
        }

    # -- section renderers ---------------------------------------------

    @staticmethod
    def _procurement_objective(f: dict[str, Any]) -> str:
        return (
            f"Secure {_get(f, 'rfq_total_quantity')} units for RFQ "
            f"'{_get(f, 'rfq_name')}' ({_get(f, 'rfq_line_count')} line(s)) from "
            f"{_get(f, 'supplier_name')} at the best achievable landed cost, while "
            "protecting lead time, quality, and payment terms."
        )

    @staticmethod
    def _supplier_position(f: dict[str, Any]) -> str:
        return (
            f"{_get(f, 'supplier_name')} ranks {_get(f, 'rank')} of "
            f"{_get(f, 'cohort_size')} scored suppliers on this scenario "
            f"(total score {_get(f, 'total_score')}). Effective unit cost is "
            f"{_get(f, 'effective_unit_cost')} {_get(f, 'currency')} against a total "
            f"landed cost of {_get(f, 'total_landed_cost')} {_get(f, 'currency')} for "
            f"the accepted quantity."
        )

    @staticmethod
    def _lead_time_concerns(f: dict[str, Any]) -> str:
        return (
            f"Quoted lead time is {_get(f, 'lead_time_days')} days. On-time delivery "
            f"history: {_get(f, 'on_time_delivery_rate')}."
        )

    @staticmethod
    def _quality_concerns(f: dict[str, Any]) -> str:
        return (
            f"Recorded quality score: {_get(f, 'quality_score')}. Recorded defect "
            f"rate: {_get(f, 'defect_rate')}."
        )

    @staticmethod
    def _spec_concerns(f: dict[str, Any]) -> str:
        unmatched = _get(f, "unmatched_rfq_lines", "none")
        uncertain = _get(f, "uncertain_quote_lines", "none")
        return (
            f"RFQ lines not matched to a quote line from this supplier: {unmatched}. "
            f"Quote lines with an unconfirmed part match: {uncertain}."
        )

    @staticmethod
    def _alternative_suppliers(f: dict[str, Any]) -> str:
        return _get(f, "alternative_suppliers_list", "no other scored suppliers on this scenario")

    @staticmethod
    def _batna(f: dict[str, Any]) -> str:
        name = f.get("best_alternative_supplier_name")
        if not name:
            return "No alternative scored supplier is available on this scenario to form a BATNA."
        return (
            f"Best alternative to a negotiated agreement: {name} (rank "
            f"{_get(f, 'best_alternative_rank')}), effective unit cost "
            f"{_get(f, 'best_alternative_effective_unit_cost')} {_get(f, 'currency')}, "
            f"total landed cost {_get(f, 'best_alternative_total_landed_cost')} "
            f"{_get(f, 'currency')}."
        )

    @staticmethod
    def _recommended_concessions(f: dict[str, Any]) -> str:
        items = f.get("recommended_concessions_list")
        return _join(
            items, empty="No rule-based concession is indicated by the current facts."
        )

    @staticmethod
    def _questions_for_clarification(f: dict[str, Any]) -> str:
        items = f.get("missing_fields_list")
        return _join(
            items,
            empty="No fields on this supplier's quote are missing; no clarification needed.",
        )

    @staticmethod
    def _email_subject(f: dict[str, Any]) -> str:
        return f"Follow-up on your quote for {_get(f, 'rfq_name')}"

    @staticmethod
    def _email_body(f: dict[str, Any]) -> str:
        lines = [
            f"Hello {_get(f, 'supplier_contact_or_name')},",
            "",
            (
                f"Thank you for your quote on {_get(f, 'rfq_name')} "
                f"({_get(f, 'rfq_total_quantity')} units)."
            ),
            (
                f"We are currently evaluating {_get(f, 'cohort_size')} supplier "
                "responses and would like to discuss your pricing and terms further."
            ),
        ]
        # email_price_target is only set when the target is a real reduction of
        # the supplier's own quoted price (brief_service._build_facts) — asking
        # to "move toward" a number above their quote invites a price increase.
        email_price_target = f.get("email_price_target")
        if email_price_target:
            line_label = f.get("primary_line_label")
            scope = f" on {line_label}" if line_label else ""
            lines.append(
                f"We are looking for a unit price closer to {email_price_target} "
                f"{_get(f, 'currency')}{scope} to remain competitive on this order."
            )
        payment_terms_days = f.get("payment_terms_days")
        cohort_best = f.get("cohort_best_payment_terms_days")
        if payment_terms_days and cohort_best and payment_terms_days != cohort_best:
            lines.append(
                f"We would also like to discuss extending payment terms beyond the "
                f"{payment_terms_days}-day terms currently quoted."
            )
        questions = f.get("missing_fields_list")
        if questions:
            lines.append(
                "A few items on your quote need clarification: " + _join(questions, empty="")
            )
        lines.extend(
            [
                "",
                "This message is a draft for internal review only and has not been sent.",
                "",
                "Best regards,",
                _get(f, "buyer_organization_name", "Procurement Team"),
            ]
        )
        return "\n".join(lines)


__all__ = ["PROVIDER_NAME", "TemplateNarrativeProvider"]
