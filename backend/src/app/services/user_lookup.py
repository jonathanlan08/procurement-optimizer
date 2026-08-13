"""Resolve user ids to display names, org-scoped.

2026-08 external review P3: RFQ status history and reviewed briefs showed
raw actor UUIDs. This helper joins `users` through `organization_memberships`
so a name is only ever resolved for members of the CALLER's organization -
an id from another org (or a stale/unknown id) resolves to None and callers
fall back to the truncated id, never a cross-org name leak.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session as SaSession

from app.models.identity import OrganizationMembership, User


def resolve_user_names(
    db: SaSession, organization_id: uuid.UUID, user_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {uid for uid in user_ids if uid is not None}
    if not wanted:
        return {}
    stmt = (
        select(User.id, User.full_name)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(
            OrganizationMembership.organization_id == organization_id,
            User.id.in_(wanted),
        )
    )
    return {row[0]: row[1] for row in db.execute(stmt).all()}


__all__ = ["resolve_user_names"]
