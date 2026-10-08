import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_accessible_drive_or_404
from app.api.errors import storage_write_failed
from app.core.security import CurrentUser, get_current_user
from app.db.models import DriveAction, UploadSession
from app.db.session import get_db
from app.schemas.data_range import DataRangeOut
from app.services.activity import (
    DRIVE_FILES_READ,
    DRIVE_WRITTEN,
    ClientContext,
    get_client_context,
    note_drive_activity,
)
from app.services.audit import log_action
from app.services.storage import (
    BlockDeleteError,
    BlockStorage,
    BlockWriteError,
    get_storage,
)
from app.services.storage_targets import get_active_targets, get_master

router = APIRouter(prefix="/data", tags=["data"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def save_data_block(
    request: Request,
    session_id: uuid.UUID = Query(..., alias="sessionId"),
    start: int = Query(..., ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    session = await db.get(UploadSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    drive = await get_accessible_drive_or_404(db, session.drive_id, user.sub)
    targets = await get_active_targets(db, session.drive_id)

    body = await request.body()
    absolute_offset = session.byte_offset + start
    if start < 0 or absolute_offset + len(body) > session.byte_offset + session.byte_length:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Write is out of the session's reserved range")

    try:
        await storage.write_range(targets, session.drive_id, absolute_offset, body)
    except BlockWriteError as exc:
        raise storage_write_failed(exc) from exc
    session.last_write_at = datetime.now(UTC)
    await note_drive_activity(db, user, session.drive_id, drive.title, DRIVE_WRITTEN, ctx)
    await db.commit()


async def _stream_with_disconnect_check(
    request: Request, chunks: AsyncIterator[bytes]
) -> AsyncIterator[bytes]:
    """Stops pulling further blocks from S3 as soon as the client disconnects
    (e.g. a <video> element aborting a Range request mid-seek), instead of
    always fetching the whole requested range regardless of whether anyone
    is still listening."""
    async for chunk in chunks:
        yield chunk
        if await request.is_disconnected():
            break


@router.get("")
async def get_data_block(
    request: Request,
    drive_id: uuid.UUID = Query(..., alias="driveId"),
    start: int = Query(..., ge=0),
    end: int = Query(..., ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> StreamingResponse:
    if end < start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "end must be >= start")

    drive = await get_accessible_drive_or_404(db, drive_id, user.sub)
    await note_drive_activity(db, user, drive_id, drive.title, DRIVE_FILES_READ, ctx, commit=True)
    available = max(0, drive.data_length - start)
    length = min(end - start, available)

    master = get_master(await get_active_targets(db, drive_id))
    chunks = storage.iter_range(master, drive_id, start, length)
    return StreamingResponse(
        _stream_with_disconnect_check(request, chunks), media_type="application/octet-stream"
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def free_data_range(
    drive_id: uuid.UUID = Query(..., alias="driveId"),
    start: int = Query(..., ge=0),
    end: int = Query(..., ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Called when a client-side file occupying [start, end) is deleted.
    Only frees storage blocks that fall entirely within the range (i.e. are
    exclusively this file's); a block the range only partially covers is left
    untouched, since other still-live data shares it."""
    if end < start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "end must be >= start")

    drive = await get_accessible_drive_or_404(db, drive_id, user.sub)
    targets = await get_active_targets(db, drive_id)
    try:
        blocks_deleted = await storage.free_range(targets, drive_id, start, end)
    except BlockDeleteError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Storage backend failed to delete some blocks: {exc}"
        ) from exc

    log_action(
        db, drive_id, user.sub, DriveAction.DELETE_DATA,
        start=start, end=end, blocks_deleted=blocks_deleted,
    )
    await note_drive_activity(db, user, drive_id, drive.title, DRIVE_WRITTEN, ctx)
    await db.commit()


@router.get("/ranges", response_model=list[DataRangeOut])
async def get_allocated_ranges(
    drive_id: uuid.UUID = Query(..., alias="driveId"),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
) -> list[DataRangeOut]:
    """Reports which byte ranges are actually backed by data in S3 right
    now (merged contiguous runs of allocated blocks) — for a client to diff
    against what its ops log says should be allocated, then DELETE /data
    whatever's left over."""
    await get_accessible_drive_or_404(db, drive_id, user.sub)
    master = get_master(await get_active_targets(db, drive_id))
    ranges = await storage.list_allocated_ranges(master, drive_id)
    return [DataRangeOut(start=s, end=e) for s, e in ranges]
