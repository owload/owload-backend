"""Reading a public drive without signing in. The link (`/public/{token}`) is the only thing that lets a request in, the drive is
read-only here, and what is read is still encrypted: opening it needs the drive's password, which the link does not carry."""
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.data import _stream_with_disconnect_check
from app.core.config import Settings, get_settings
from app.db.models import Drive, User
from app.db.session import get_db
from app.services.access import PUBLIC, public_read_allowed
from app.services.activity import ClientContext, get_client_context
from app.services.storage import BlockStorage, get_storage
from app.services.storage_targets import get_active_targets, get_master

router = APIRouter(prefix="/public/{token}", tags=["public"])

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


class PublicDriveOut(BaseModel):
    """What a viewer needs to set the drive up. The owner is shown by name; nothing about who else has access."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str
    key_nonce: str = Field(alias="keyNonce")
    counter_nonce: str = Field(alias="counterNonce")
    created_timestamp: int = Field(alias="createdTimestamp")
    data_length: int = Field(alias="dataLength")
    owner_name: str | None = Field(default=None, alias="ownerName")


async def get_public_drive(
    token: str,
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
    settings: Settings = Depends(get_settings),
) -> Drive:
    """The public drive of the link, or 404 (the same answer for a wrong, an old and a made-up link). Slows down whoever asks too much."""
    if not public_read_allowed(ctx.ip, settings.public_reads_per_minute):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests", headers={"Retry-After": "60"})
    if not _TOKEN_PATTERN.match(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drive not found")
    drive = await db.scalar(select(Drive).where(Drive.public_token == token, Drive.visibility == PUBLIC))
    if drive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drive not found")
    return drive


@router.get("", response_model=PublicDriveOut, response_model_by_alias=True)
async def get_public_drive_info(
    response: Response,
    drive: Drive = Depends(get_public_drive),
    db: AsyncSession = Depends(get_db),
) -> PublicDriveOut:
    for name, value in _NO_STORE.items():
        response.headers[name] = value
    owner = await db.get(User, drive.owner_user_id)
    return PublicDriveOut(
        id=str(drive.id),
        title=drive.title,
        key_nonce=drive.key_nonce,
        counter_nonce=drive.counter_nonce,
        created_timestamp=int(drive.created_at.timestamp() * 1000),
        data_length=drive.data_length,
        owner_name=(owner.name or None) if owner else None,
    )


@router.get("/ops/{start_byte_pos}")
async def get_public_operations(
    start_byte_pos: int,
    drive: Drive = Depends(get_public_drive),
) -> Response:
    if start_byte_pos < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "startBytePos must not be negative")
    return Response(content=bytes(drive.ops_data[start_byte_pos:]), media_type="application/octet-stream", headers=_NO_STORE)


@router.get("/data")
async def get_public_data(
    request: Request,
    start: int = Query(..., ge=0),
    end: int = Query(..., ge=0),
    drive: Drive = Depends(get_public_drive),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
) -> StreamingResponse:
    if end < start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "end must be >= start")
    length = min(end - start, max(0, drive.data_length - start))
    drive_id: uuid.UUID = drive.id
    master = get_master(await get_active_targets(db, drive_id))
    chunks = storage.iter_range(master, drive_id, start, length)
    return StreamingResponse(
        _stream_with_disconnect_check(request, chunks), media_type="application/octet-stream", headers=_NO_STORE
    )
