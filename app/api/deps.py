import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive
from app.db.session import get_db
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


async def get_accessible_drive_or_404(db: AsyncSession, drive_id: uuid.UUID, user_sub: str) -> Drive:
    """Plain helper (not a FastAPI dependency) — call directly from route bodies.
    Returns 404 for both "doesn't exist" and "no access" so the two are
    indistinguishable to an unauthorized caller."""
    drive = await db.get(Drive, drive_id)
    if drive is None or (drive.owner_user_id != user_sub and user_sub not in drive.acl):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drive not found")
    return drive
