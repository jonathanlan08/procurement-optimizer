"""Shared CSV/XLSX formula-injection escape (docs/planning/03-api-contract.md
§5's spirit - untrusted free text must never become active spreadsheet
content).

Report content includes supplier-provided free text (supplier names, quote
notes, negotiation-brief prose) with no format restriction of its own. A
value like `=HYPERLINK("http://evil")` opened as a cell in Excel/Google
Sheets/LibreOffice is executed, not displayed - the classic CSV/XLSX
"formula injection" vector (OWASP). The standard mitigation is a leading
apostrophe: every major spreadsheet application treats a cell beginning
with `'` as forced plain text, both when a user types it and when a CSV
importer/`.xlsx` reader encounters it.

`escape_formula_cell` is the ONE place this check is written; both
`app/reports/csv_renderer.py` and `app/reports/xlsx_renderer.py` call it on
every cell before it is written. This matters even more for the XLSX path
than for CSV: `openpyxl` itself auto-detects a leading `=` on a plain string
assigned to `Cell.value` and stores it as a live formula (`data_type='f'`)
unless the escape has already removed that prefix - so skipping this here
would not just be a spreadsheet-app risk, it would make this codebase's own
writer produce an executable formula.
"""

from __future__ import annotations

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_formula_cell(value: str) -> str:
    """Prefix `value` with `'` when it starts with a character a spreadsheet
    application could interpret as the start of a formula/command
    (`=`, `+`, `-`, `@`) or a whitespace-based injection technique
    (tab, carriage return). The check runs on the LEADING-WHITESPACE-STRIPPED
    value - matching the ingress check in `app.importing.part_import_parser` -
    so `" =HYPERLINK(...)"` and `"\\n=..."` are escaped too (2026-08 security
    audit, LOW-9). Anything else is returned unchanged."""
    if value.lstrip().startswith(_DANGEROUS_PREFIXES) or value.startswith(
        _DANGEROUS_PREFIXES
    ):
        return "'" + value
    return value


__all__ = ["escape_formula_cell"]
