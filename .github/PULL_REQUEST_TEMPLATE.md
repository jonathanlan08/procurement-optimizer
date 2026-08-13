## Summary

<!-- What does this PR do and why? 1-3 bullet points. -->

-

## Changes

<!-- List the meaningful changes. Call out any new dependency, new route, or
     migration explicitly - these require extra review per the working rules
     in docs/planning/09-task-decomposition.md §10. -->

-

## Tests run

<!-- Paste the exact command(s) you ran locally and confirm they passed. -->

```
uv run pytest -m "not integration"   # unit + component + contract, no DB required
uv run pytest                        # full suite incl. integration (pgserver or docker compose postgres)
uv run ruff check .
uv run mypy
```

## Checklist

- [ ] No secrets, credentials, or API keys committed (checked diff, not just `.env`)
- [ ] New/changed migrations reviewed by hand (autogenerate output is never merged
      unread); migrations touching tenancy or composite FKs flagged for careful review
- [ ] Organization isolation respected (queries scoped through `OrgScopedRepository`;
      no cross-org data path introduced or widened)
- [ ] Decimal-as-string respected (money/quantity fields serialize as strings in
      API schemas, no `float` introduced in `app/domain` or money paths)
