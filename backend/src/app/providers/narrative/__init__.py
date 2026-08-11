"""Negotiation-brief narrative providers (docs/SPEC.md §Negotiation brief).

`AiNarrativeProvider` (base.py) is the Protocol every provider implements.
`TemplateNarrativeProvider` (template.py) is the only implementation shipped
in v0.1 — deterministic, offline, `is_generated=False` (SPEC §External-
service strategy: "Public demo works without any paid AI key"). The
config-selected factory (`app.core.config.NarrativeProviderKind` ->
concrete provider) lives in `app.services.brief_service.
build_narrative_provider`, mirroring `app.services.extraction_service.
build_extraction_provider`'s placement, not here.
"""

from __future__ import annotations

from app.providers.narrative.base import NARRATIVE_SECTION_KEYS, AiNarrativeProvider
from app.providers.narrative.template import TemplateNarrativeProvider

__all__ = ["NARRATIVE_SECTION_KEYS", "AiNarrativeProvider", "TemplateNarrativeProvider"]
