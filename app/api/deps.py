import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive
from app.db.session import get_db
from app.services.access import ROLE_LABELS, Role, role_of
from app.services.activity import is_session_revoked

# The detail of the 401 that tells the client its session was signed out (the client then signs out too).
SESSION_REVOKED_DETAIL = "Session was signed out"


async def require_active_session(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Refuses a token of a session the user has signed out from another device. Keycloak still honours such a token until
    it expires, so the backend does the refusing; applied to every router in app/main.py."""
    if user.session_id and await is_session_revoked(db, user.session_id):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, SESSION_REVOKED_DETAIL)


async def get_drive_and_role(
    db: AsyncSession, drive_id: uuid.UUID, user_sub: str, minimum: Role = Role.READER
) -> tuple[Drive, Role]:
    """Plain helper (not a FastAPI dependency) — call directly from route bodies. Returns the drive and the role of the user on
    it. 404 for both "doesn't exist" and "not yours to open", so the two are indistinguishable to someone without access; 403 for
    someone who has the drive but not the role `minimum` needs."""
    drive = await db.get(Drive, drive_id)
    role = role_of(drive, user_sub) if drive is not None else None
    if drive is None or role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drive not found")
    if role < minimum:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This needs the role of {ROLE_LABELS[minimum]} on the drive")
    return drive, role


async def get_accessible_drive_or_404(
    db: AsyncSession, drive_id: uuid.UUID, user_sub: str, minimum: Role = Role.READER
) -> Drive:
    """The drive, when the user has at least the role `minimum` on it (see get_drive_and_role)."""
    drive, _ = await get_drive_and_role(db, drive_id, user_sub, minimum)
    return drive
