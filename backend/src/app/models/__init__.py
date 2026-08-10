"""All ORM models. Importing this package registers every table on Base.metadata,
which Alembic's env.py relies on for autogenerate comparisons."""

from app.models.audit import JOB_STATE_ENUM, AuditEvent, Job, JobState
from app.models.base import (
    Base,
    OrgOwnedMixin,
    org_identity_constraint,
)
from app.models.identity import (
    ROLE_ENUM,
    Organization,
    OrganizationMembership,
    Role,
    Session,
    User,
)
from app.models.suppliers import Supplier, SupplierContact, SupplierPerformanceRecord

__all__ = [
    "JOB_STATE_ENUM",
    "ROLE_ENUM",
    "AuditEvent",
    "Base",
    "Job",
    "JobState",
    "OrgOwnedMixin",
    "Organization",
    "OrganizationMembership",
    "Role",
    "Session",
    "Supplier",
    "SupplierContact",
    "SupplierPerformanceRecord",
    "User",
    "org_identity_constraint",
]
