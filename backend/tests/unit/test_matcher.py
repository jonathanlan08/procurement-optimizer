"""Unit tests for the pure part-matching domain logic
(docs/planning/04-document-pipeline.md §10, app/domain/matching/matcher.py).

Each of the five strategies is exercised in isolation, then priority
ordering/determinism/dedup across strategies, then the fuzzy threshold
boundary and explanation-string content. See matcher.py's own module
docstring for why the implemented confidence table/priority order/fuzzy
formula follow §10's literal text rather than this task's own paraphrase of
it - these tests assert the §10 numbers.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from app.domain.matching.matcher import (
    CatalogPart,
    LineTexts,
    MatchCandidate,
    MatchConfig,
    generate_candidates,
)
from app.models.documents import MatchStrategy

DEFAULT_CONFIG = MatchConfig()


def _part(
    *,
    part_id: UUID | None = None,
    internal_part_number: str,
    manufacturer_part_number: str | None = None,
    normalized_key: str | None = None,
    name: str | None = None,
    alternative_of: tuple[UUID, ...] = (),
) -> CatalogPart:
    return CatalogPart(
        part_id=part_id or uuid4(),
        internal_part_number=internal_part_number,
        manufacturer_part_number=manufacturer_part_number,
        normalized_key=normalized_key or internal_part_number.lower(),
        name=name or internal_part_number,
        alternative_of=alternative_of,
    )


def _only(candidates: tuple[MatchCandidate, ...], strategy: MatchStrategy) -> MatchCandidate:
    matches = [c for c in candidates if c.strategy is strategy]
    assert len(matches) == 1, f"expected exactly one {strategy} candidate, got {matches}"
    return matches[0]


class TestInternalPnStrategy:
    def test_exact_case_insensitive_match(self) -> None:
        part = _part(internal_part_number="ACME-100")
        candidates = generate_candidates(
            LineTexts(part_number_text="acme-100", description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        cand = _only(candidates, MatchStrategy.INTERNAL_PN)
        assert cand.part_id == part.part_id
        assert cand.confidence == Decimal("1.00")
        assert "acme-100" in cand.explanation
        assert "ACME-100" in cand.explanation

    def test_no_match_when_different(self) -> None:
        part = _part(internal_part_number="ACME-100")
        candidates = generate_candidates(
            LineTexts(part_number_text="ACME-200", description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        assert not any(c.strategy is MatchStrategy.INTERNAL_PN for c in candidates)

    def test_blank_part_number_text_yields_no_candidates(self) -> None:
        part = _part(internal_part_number="ACME-100")
        candidates = generate_candidates(
            LineTexts(part_number_text=None, description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        assert not any(c.strategy is MatchStrategy.INTERNAL_PN for c in candidates)


class TestMpnStrategy:
    def test_exact_after_separator_stripping(self) -> None:
        part = _part(internal_part_number="X-1", manufacturer_part_number="CR0805-10K")
        candidates = generate_candidates(
            LineTexts(part_number_text="CR0805 10K", description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        cand = _only(candidates, MatchStrategy.MPN)
        assert cand.part_id == part.part_id
        assert cand.confidence == Decimal("0.97")
        assert "CR0805 10K" in cand.explanation
        assert "CR0805-10K" in cand.explanation

    def test_slash_and_space_separators_also_strip(self) -> None:
        part = _part(internal_part_number="X-1", manufacturer_part_number="AB/12 34")
        candidates = generate_candidates(
            LineTexts(part_number_text="ab1234", description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        cand = _only(candidates, MatchStrategy.MPN)
        assert cand.confidence == Decimal("0.97")

    def test_no_manufacturer_part_number_no_match(self) -> None:
        part = _part(internal_part_number="X-1", manufacturer_part_number=None)
        candidates = generate_candidates(
            LineTexts(part_number_text="CR0805-10K", description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        assert not any(c.strategy is MatchStrategy.MPN for c in candidates)


class TestNormalizedTextStrategy:
    def test_punctuation_insensitive_normalized_match(self) -> None:
        # 'ACME-100 Rev.B' vs a catalog entry whose normalized_key is
        # 'acme100revb' but whose raw internal_part_number does NOT equal
        # the query case-insensitively (so strategy 1 stays isolated out).
        part = _part(
            internal_part_number="ACME100REVB-CATALOG",
            normalized_key="acme100revb",
        )
        candidates = generate_candidates(
            LineTexts(part_number_text="ACME-100 Rev.B", description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        assert not any(c.strategy is MatchStrategy.INTERNAL_PN for c in candidates)
        cand = _only(candidates, MatchStrategy.NORMALIZED_TEXT)
        assert cand.part_id == part.part_id
        assert cand.confidence == Decimal("0.85")
        assert "acme100revb" in cand.explanation

    def test_no_match_when_normalized_keys_differ(self) -> None:
        part = _part(internal_part_number="ZZZ-999", normalized_key="zzz999")
        candidates = generate_candidates(
            LineTexts(part_number_text="ACME-100", description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        assert not any(c.strategy is MatchStrategy.NORMALIZED_TEXT for c in candidates)


class TestAlternativeStrategy:
    def test_alternative_resolves_to_canonical_part(self) -> None:
        canonical = _part(internal_part_number="CANON-1", name="Canonical Widget")
        alternative = _part(
            internal_part_number="ALT-9",
            name="Alternate Widget",
            alternative_of=(canonical.part_id,),
        )
        candidates = generate_candidates(
            LineTexts(part_number_text="ALT-9", description_text=None),
            (canonical, alternative),
            DEFAULT_CONFIG,
        )
        cand = _only(candidates, MatchStrategy.ALTERNATIVE)
        assert cand.part_id == canonical.part_id  # candidate is for the CANONICAL part
        assert cand.confidence == Decimal("0.90")
        assert "ALT-9" in cand.explanation
        assert "CANON-1" in cand.explanation

    def test_no_alternative_relationship_no_candidate(self) -> None:
        canonical = _part(internal_part_number="CANON-1")
        unrelated = _part(internal_part_number="ALT-9", alternative_of=())
        candidates = generate_candidates(
            LineTexts(part_number_text="ALT-9", description_text=None),
            (canonical, unrelated),
            DEFAULT_CONFIG,
        )
        assert not any(c.strategy is MatchStrategy.ALTERNATIVE for c in candidates)


class TestFuzzyStrategy:
    """Threshold boundary fixed at MatchConfig's default (0.82) - see
    matcher.py's own module docstring for why 0.82, not §10's stated 0.80.
    Query/corpus pairs below were found empirically to score just under and
    just over that boundary (~0.81 / ~0.83, per this task's own required
    test coverage)."""

    def test_below_threshold_excluded(self) -> None:
        # token_set_ratio("housin spring aluminum", "spring steel aluminum
        # housing X-73") == 81.08 (< 82 threshold) - verified empirically.
        part = _part(internal_part_number="X-73", name="spring steel aluminum housing")
        candidates = generate_candidates(
            LineTexts(part_number_text=None, description_text="housin spring aluminum"),
            (part,),
            DEFAULT_CONFIG,
        )
        assert not any(c.strategy is MatchStrategy.FUZZY for c in candidates)

    def test_at_or_above_threshold_included(self) -> None:
        # token_set_ratio("valve clamp housi", "clamp valve housing X-99")
        # == 82.93 (>= 82 threshold) - verified empirically.
        part = _part(internal_part_number="X-99", name="clamp valve housing")
        candidates = generate_candidates(
            LineTexts(part_number_text=None, description_text="valve clamp housi"),
            (part,),
            DEFAULT_CONFIG,
        )
        cand = _only(candidates, MatchStrategy.FUZZY)
        assert cand.part_id == part.part_id
        assert Decimal("0") < cand.confidence <= Decimal("0.80")

    def test_confidence_capped_at_point_eight_even_for_perfect_match(self) -> None:
        part = _part(internal_part_number="X-3", name="identical text match")
        candidates = generate_candidates(
            LineTexts(part_number_text=None, description_text="identical text match"),
            (part,),
            DEFAULT_CONFIG,
        )
        cand = _only(candidates, MatchStrategy.FUZZY)
        assert cand.confidence == Decimal("0.80")

    def test_blank_line_texts_yield_no_fuzzy_candidates(self) -> None:
        part = _part(internal_part_number="X-4", name="widget connector housing spring")
        candidates = generate_candidates(
            LineTexts(part_number_text=None, description_text=None),
            (part,),
            DEFAULT_CONFIG,
        )
        assert candidates == ()


class TestPriorityOrderingAndDeterminism:
    def test_highest_confidence_strategy_ranks_first(self) -> None:
        exact = _part(internal_part_number="ACME-100")
        mpn_only = _part(internal_part_number="OTHER-1", manufacturer_part_number="ACME-100")
        candidates = generate_candidates(
            LineTexts(part_number_text="ACME-100", description_text=None),
            (mpn_only, exact),
            DEFAULT_CONFIG,
        )
        # internal_pn (1.00) must outrank mpn (0.97) regardless of catalog order.
        assert candidates[0].strategy is MatchStrategy.INTERNAL_PN
        assert candidates[0].part_id == exact.part_id
        confidences = [c.confidence for c in candidates]
        assert confidences == sorted(confidences, reverse=True)

    def test_ties_broken_by_internal_part_number_then_part_id(self) -> None:
        # Two catalog parts with identical fuzzy-match text: score ties, so
        # the (internal_part_number, part_id) tail of the sort key decides.
        part_a = _part(internal_part_number="B-PART", name="shared bracket text")
        part_b = _part(internal_part_number="A-PART", name="shared bracket text")
        candidates = generate_candidates(
            LineTexts(part_number_text=None, description_text="shared bracket text"),
            (part_a, part_b),
            DEFAULT_CONFIG,
        )
        fuzzy = [c for c in candidates if c.strategy is MatchStrategy.FUZZY]
        assert len(fuzzy) == 2
        assert fuzzy[0].confidence == fuzzy[1].confidence
        assert fuzzy[0].part_id == part_b.part_id  # "A-PART" sorts before "B-PART"

    def test_deterministic_across_repeated_calls(self) -> None:
        parts = (
            _part(internal_part_number="ACME-100"),
            _part(internal_part_number="OTHER-1", manufacturer_part_number="ACME-100"),
            _part(internal_part_number="ZED-1", name="acme widget bracket"),
        )
        line_texts = LineTexts(part_number_text="ACME-100", description_text="acme widget")
        first = generate_candidates(line_texts, parts, DEFAULT_CONFIG)
        second = generate_candidates(line_texts, parts, DEFAULT_CONFIG)
        assert first == second

    def test_max_candidates_caps_result_length(self) -> None:
        parts = tuple(
            _part(internal_part_number=f"WIDGET-{i}", name="widget bracket assembly text")
            for i in range(5)
        )
        config = MatchConfig(max_candidates=2)
        candidates = generate_candidates(
            LineTexts(part_number_text=None, description_text="widget bracket assembly text"),
            parts,
            config,
        )
        assert len(candidates) == 2


class TestDedup:
    def test_duplicate_catalog_entry_for_same_part_does_not_duplicate_candidate(self) -> None:
        part = _part(internal_part_number="ACME-100")
        candidates = generate_candidates(
            LineTexts(part_number_text="ACME-100", description_text=None),
            (part, part),  # same CatalogPart present twice
            DEFAULT_CONFIG,
        )
        internal_pn_hits = [c for c in candidates if c.strategy is MatchStrategy.INTERNAL_PN]
        assert len(internal_pn_hits) == 1
