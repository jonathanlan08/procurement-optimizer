"""Pure parsing/validation layer for part imports. No database access anywhere
in this package — see app.services.part_import_service for the DB-dependent
second pass (unit_code resolution, existing-part duplicate detection,
persistence)."""
