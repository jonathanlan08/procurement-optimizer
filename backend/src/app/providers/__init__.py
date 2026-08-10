"""Provider packages: pluggable external-service boundaries.

Each provider kind (extraction, OCR, storage, narrative, ...) lives in its own
subpackage behind a small Protocol so tests and demo mode never depend on a
real network call (SPEC "External-service strategy"). `app.providers.fx` is
the currency-normalization provider (docs/planning/05-calculation-methodology.md
§4); see that subpackage's `base.py` for the Protocol and `synthetic.py` for
the deterministic demo/test implementation.
"""

from __future__ import annotations
