import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Drive


async def get_accessible_drive_or_404(db: AsyncSession, drive_id: uuid.UUID, user_sub: str) -> Drive:
    """Plain helper (not a FastAPI dependency) — call directly from route bodies.
    Returns 404 for both "doesn't exist" and "no access" so the two are
    indistinguishable to an unauthorized caller."""
    drive = await db.get(Drive, drive_id)
    if drive is None or (drive.owner_user_id != user_sub and user_sub not in drive.acl):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drive not found")
    return drive
