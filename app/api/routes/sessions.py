import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_accessible_drive_or_404
from app.core.config import Settings, get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive, DriveAction, SessionStatus, UploadSession
from app.db.session import get_db
from app.schemas.session import SessionInfoOut
from app.services.access import Role
from app.services.activity import (
    DRIVE_WRITTEN,
    ClientContext,
    get_client_context,
    note_drive_activity,
)
from app.services.audit import log_action
from app.services.quota import ensure_room
from app.services.stored_bytes import mark_stale

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _to_session_info(session: UploadSession) -> SessionInfoOut:
    return SessionInfoOut(
        session_id=str(session.id),
        byte_offset=session.byte_offset,
        byte_length=session.byte_length,
        drive_id=str(session.drive_id),
        created_timestamp=int(session.created_at.timestamp() * 1000),
        last_write_timestamp=int(session.last_write_at.timestamp() * 1000) if session.last_write_at else 0,
        status=session.status,
    )


@router.post("", response_model=SessionInfoOut, status_code=status.HTTP_201_CREATED)
async def start_upload_session(
    drive_id: uuid.UUID = Query(..., alias="driveId"),
    byte_length: int = Query(..., alias="byteLength", ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
    settings: Settings = Depends(get_settings),
) -> SessionInfoOut:
    drive = await get_accessible_drive_or_404(db, drive_id, user.sub, Role.WRITER)
    # Whoever writes, the drive's limit and its owner's must have room for it (decisions/0036).
    await ensure_room(db, drive, user.sub, byte_length, settings)

    # Atomically reserve [old data_length, old data_length + byte_length) for this session.
    result = await db.execute(
        update(Drive)
        .where(Drive.id == drive_id)
        .values(data_length=Drive.data_length + byte_length)
        .returning(Drive.data_length)
    )
    new_length = result.scalar_one()
    byte_offset = new_length - byte_length

    session = UploadSession(
        drive_id=drive_id,
        byte_offset=byte_offset,
        byte_length=byte_length,
        status=SessionStatus.ACTIVE,
    )
    db.add(session)
    await db.flush()  # populates session.id/created_at via RETURNING, before we log against it

    log_action(
        db, drive_id, user.sub, DriveAction.START_SESSION,
        session_id=str(session.id), start=byte_offset, end=byte_offset + byte_length,
    )
    await note_drive_activity(db, user, drive_id, drive.title, DRIVE_WRITTEN, ctx)
    await db.commit()
    return _to_session_info(session)


@router.post("/finalize/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def finish_upload_session(
    session_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    session = await db.get(UploadSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    drive = await get_accessible_drive_or_404(db, session.drive_id, user.sub, Role.WRITER)
    if session.status in (SessionStatus.FINISHED, SessionStatus.STALE):
        raise HTTPException(status.HTTP_409_CONFLICT, "Session is closed")

    session.status = SessionStatus.FINISHED
    mark_stale(drive)
    log_action(
        db, session.drive_id, user.sub, DriveAction.FINALIZE_SESSION,
        session_id=str(session.id), start=session.byte_offset, end=session.byte_offset + session.byte_length,
    )
    await note_drive_activity(db, user, session.drive_id, drive.title, DRIVE_WRITTEN, ctx)
    await db.commit()
