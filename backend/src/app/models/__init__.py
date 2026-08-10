"""All ORM models. Importing this package registers every table on Base.metadata,
which Alembic's env.py relies on for autogenerate comparisons."""

from app.models.audit import JOB_STATE_ENUM, AuditEvent, Job, JobState
from app.models.base import (
    Base,
    OrgOwnedMixin,
    org_identity_constraint,
)
from app.models.boms import BOM_STATUS_ENUM, BillOfMaterialLine, BillOfMaterials, BomStatus
from app.models.identity import (
    ROLE_ENUM,
    Organization,
    OrganizationMembership,
    Role,
    Session,
    User,
)
from app.models.part_imports import (
    IMPORT_STATE_ENUM,
    PART_IMPORT_FORMAT_ENUM,
    PartImportBatch,
    PartImportFormat,
    PartImportRow,
    PartImportRowDisposition,
    PartImportState,
)
from app.models.parts import Part, PartAlternative
from app.models.suppliers import Supplier, SupplierContact, SupplierPerformanceRecord
from app.models.units import DIMENSION_ENUM, UnitConversion, UnitDefinition, UnitDimension

__all__ = [
    "BOM_STATUS_ENUM",
    "DIMENSION_ENUM",
    "IMPORT_STATE_ENUM",
    "JOB_STATE_ENUM",
    "PART_IMPORT_FORMAT_ENUM",
    "ROLE_ENUM",
    "AuditEvent",
    "Base",
    "BillOfMaterialLine",
    "BillOfMaterials",
    "BomStatus",
    "Job",
    "JobState",
    "OrgOwnedMixin",
    "Organization",
    "OrganizationMembership",
    "Part",
    "PartAlternative",
    "PartImportBatch",
    "PartImportFormat",
    "PartImportRow",
    "PartImportRowDisposition",
    "PartImportState",
    "Role",
    "Session",
    "Supplier",
    "SupplierContact",
    "SupplierPerformanceRecord",
    "UnitConversion",
    "UnitDefinition",
    "UnitDimension",
    "User",
    "org_identity_constraint",
]
