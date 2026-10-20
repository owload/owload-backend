"""Asking for access to a drive one is not on (owload-docs/decisions/0037).

These three calls are the only ones that tell a signed-in person that a drive exists when they are not on it (every other route keeps the single
404 for a stranger, decision 0030): the drive's title and whether they have asked, nothing else. The admins answer in the drive's settings
(app/api/routes/access.py)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive, DriveAccessRequest, User
from app.db.session import get_db
from app.schemas.access import AccessRequestIn, MyAccessRequestOut
from app.services import access
from app.services.mailer import access_request_mail, mail_enabled, send_mail

router = APIRouter(prefix="/drives/{drive_id}/access-request", tags=["access-requests"])


async def _drive_or_404(db: AsyncSession, drive_id: uuid.UUID) -> Drive:
    drive = await db.get(Drive, drive_id)
    if drive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drive not found")
    return drive


def _state(drive: Drive, has_access: bool, request: DriveAccessRequest | None, settings: Settings) -> MyAccessRequestOut:
    return MyAccessRequestOut(
        drive_id=str(drive.id),
        has_access=has_access,
        title=drive.title,
        requested=request is not None,
        requested_at=request.created_at if request else None,
        message=request.message if request else None,
        mail_enabled=mail_enabled(settings),
    )


async def _my_request(db: AsyncSession, drive_id: uuid.UUID, user_id: str) -> DriveAccessRequest | None:
    return await db.scalar(
        select(DriveAccessRequest).where(DriveAccessRequest.drive_id == drive_id, DriveAccessRequest.user_id == user_id)
    )


@router.get("", response_model=MyAccessRequestOut, response_model_by_alias=True)
async def get_my_request(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MyAccessRequestOut:
    """Whether the drive exists, what it is called, and whether the signed-in person has asked for it. 404 only for a drive that does not exist."""
    drive = await _drive_or_404(db, drive_id)
    if access.role_of(drive, user.sub) is not None:
        return _state(drive, True, None, settings)
    return _state(drive, False, await _my_request(db, drive_id, user.sub), settings)


@router.post("", response_model=MyAccessRequestOut, response_model_by_alias=True, status_code=status.HTTP_201_CREATED)
async def ask_for_access(
    drive_id: uuid.UUID,
    payload: AccessRequestIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MyAccessRequestOut:
    """Asks the owner and the admins of the drive to let the person in. Asking again replaces the message. 409 for someone who has the drive."""
    drive = await _drive_or_404(db, drive_id)
    if access.role_of(drive, user.sub) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "You already have access to this drive")
    if not access.access_request_allowed(user.sub):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests for access; try again later")

    message = (payload.message or "").strip() or None
    request = await _my_request(db, drive_id, user.sub)
    if request is None:
        request = DriveAccessRequest(id=access.new_id(), drive_id=drive_id, user_id=user.sub, message=message)
        db.add(request)
    else:
        request.message = message
    await db.commit()
    await db.refresh(request)

    # the owner and the admins are told; a mail server that is down must not fail the request (it waits in the settings)
    me = await db.get(User, user.sub)
    name = (me.name if me and me.name else None) or user.email or "Someone"
    admin_ids = {drive.owner_user_id} | {
        person for person, role in (drive.acl or {}).items()
        if access.has_people(drive.visibility) and access.parse_role(role) >= access.Role.ADMIN
    }
    recipients = (await db.execute(select(User.email).where(User.id.in_(admin_ids)))).scalars().all()
    link = f"{settings.app_url.rstrip('/')}/drive/{drive.id}/settings"
    for address in recipients:
        subject, body, html_body = access_request_mail(name, user.email or (me.email if me else ""), drive.title, message, link, address)
        await send_mail(settings, address, subject, body, html_body)
    return _state(drive, False, request, settings)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_request(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Takes back the person's own request. Nothing to take back is not an error."""
    await _drive_or_404(db, drive_id)
    await db.execute(delete(DriveAccessRequest).where(DriveAccessRequest.drive_id == drive_id, DriveAccessRequest.user_id == user.sub))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
