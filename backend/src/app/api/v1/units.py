"""Unit-definition listing: the global catalogue plus this org's own units.

Read-only in v0.1 (user-defined unit management is roadmap); exists so the UI
can resolve unit_definition_id to a human-readable code/name.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.api.deps import DbDep, Principal, require_role
from app.models.identity import Role
from app.models.units import UnitDefinition

router = APIRouter(prefix="/units", tags=["units"])


class UnitDefinitionResponse(BaseModel):
    id: str
    code: str
    name: str
    dimension: str
    is_global: bool


class UnitListResponse(BaseModel):
    items: list[UnitDefinitionResponse]


@router.get("")
def list_units(
    db: DbDep,
    principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> UnitListResponse:
    rows = (
        db.execute(
            select(UnitDefinition)
            .where(
                or_(
                    UnitDefinition.organization_id.is_(None),
                    UnitDefinition.organization_id == principal.organization_id,
                )
            )
            .order_by(UnitDefinition.dimension, UnitDefinition.code)
        )
        .scalars()
        .all()
    )
    return UnitListResponse(
        items=[
            UnitDefinitionResponse(
                id=str(u.id),
                code=u.code,
                name=u.display_name,
                dimension=str(u.dimension),
                is_global=u.organization_id is None,
            )
            for u in rows
        ]
    )
