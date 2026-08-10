"""FX rate providers (docs/planning/05-calculation-methodology.md §4).

`FxRateProvider` (base.py) is the Protocol every provider implements.
`SyntheticFxProvider` (synthetic.py) is the only implementation shipped in
v0.1 — deterministic, offline, clearly labelled synthetic (SPEC §9: "Public
demo ships deterministic synthetic exchange rates; automated tests must not
depend on external services"). There is no config-selected provider kind
(unlike `ExtractionProviderKind` etc. in `app.core.config`): the fixture
provider is chosen explicitly in code by the service/seed layer, not switched
by environment — see `app.core.config` module docstring / this task's brief.
"""

from __future__ import annotations

from app.providers.fx.base import FxRateProvider
from app.providers.fx.synthetic import SyntheticFxProvider

__all__ = ["FxRateProvider", "SyntheticFxProvider"]
