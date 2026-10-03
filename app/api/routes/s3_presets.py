import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, get_current_user
from app.db.models import S3Preset
from app.db.session import get_db
from app.schemas.s3_preset import S3PresetOut
from app.schemas.storage_target import ConnectionTestOut
from app.services.storage import BlockStorage, get_storage
from app.services.storage_targets import resolve_preset

router = APIRouter(prefix="/s3-presets", tags=["s3-presets"])


@router.get("", response_model=list[S3PresetOut])
async def list_s3_presets(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[S3PresetOut]:
    """The admin-curated preset pool (owload-docs/decisions/0011) — no secrets,
    just enough for a client to list/filter by tier and pass an id back as
    presetId (POST /drives, POST /drives/{driveId}/storage-targets)."""
    result = await db.execute(select(S3Preset))
    return [
        S3PresetOut(id=str(p.id), label=p.label, tier=p.tier.lower())
        for p in result.scalars().all()
    ]


@router.post("/{preset_id}/test-connection", response_model=ConnectionTestOut)
async def test_s3_preset(
    preset_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
) -> ConnectionTestOut:
    """Tests a preset's own reachability directly, before it's ever
    attached to any drive — a preset being admin-curated doesn't mean it's
    actually up right now (wrong credentials, bucket renamed/deleted,
    endpoint down). Same {ok, error} shape and always-200 contract as the
    other two test-connection endpoints (owload-docs/epics/0012 task 0009)."""
    preset = await db.get(S3Preset, preset_id)
    if preset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Storage preset not found")
    ok, error = await storage.test_connection(resolve_preset(preset))
    return ConnectionTestOut(ok=ok, error=error)
