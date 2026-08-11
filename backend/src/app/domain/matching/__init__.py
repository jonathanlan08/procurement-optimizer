"""Pure part-matching domain logic (docs/planning/04-document-pipeline.md
§10). See `matcher.py` for the full module docstring."""

from app.domain.matching.matcher import (
    CatalogPart,
    LineTexts,
    MatchCandidate,
    MatchConfig,
    generate_candidates,
)

__all__ = [
    "CatalogPart",
    "LineTexts",
    "MatchCandidate",
    "MatchConfig",
    "generate_candidates",
]
