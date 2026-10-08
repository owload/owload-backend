import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.models import User, UserAvatar, UserEvent, UserSession
from app.db.session import get_db
from app.schemas.user import (
    AVATAR_UPLOAD_MAX_BYTES,
    AvatarOut,
    SaveUserBasicInfoIn,
    SetAvatarPresetIn,
    UserActivityPageOut,
    UserBasicInfoOut,
    UserEventOut,
    UserSessionOut,
)
from app.services import activity
from app.services.activity import ClientContext, get_client_context
from app.services.avatar_image import AvatarImageError, make_avatar_jpeg

router = APIRouter(prefix="/userinfo", tags=["userinfo"])


@router.get("/search/{search}", response_model=list[UserBasicInfoOut])
async def find_users(
    search: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserBasicInfoOut]:
    pattern = f"%{search}%"
    result = await db.execute(select(User).where(or_(User.email.ilike(pattern), User.name.ilike(pattern))))
    return [
        UserBasicInfoOut(user_id=row.id, email=row.email, name=row.name) for row in result.scalars().all()
    ]


async def _avatar_out(db: AsyncSession, user_id: str) -> AvatarOut:
    """What the user chose as the picture, without loading the image bytes."""
    result = await db.execute(
        select(UserAvatar.preset, UserAvatar.updated_at).where(UserAvatar.user_id == user_id)
    )
    found = result.first()
    if found is None:
        return AvatarOut(kind="default")
    preset, updated_at = found
    if preset is not None:
        return AvatarOut(kind="preset", preset=preset)
    return AvatarOut(kind="upload", version=int(updated_at.timestamp() * 1000))


def _image_content_type(data: bytes) -> str | None:
    """PNG, JPEG or WebP, told by the first bytes. The server does not decode the image: it only keeps it, and the
    client shows it in an <img>, where a file of another kind would simply not draw."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _ensure_user_row(db: AsyncSession, user: CurrentUser) -> None:
    if await db.get(User, user.sub) is None:
        db.add(User(id=user.sub, email=user.email or "", name=_token_name(user)))
        await db.flush()


@router.put("/avatar/preset", status_code=status.HTTP_204_NO_CONTENT)
async def set_avatar_preset(
    payload: SetAvatarPresetIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Chooses one of the preset pictures (this drops an uploaded image)."""
    await _ensure_user_row(db, user)
    row = await db.get(UserAvatar, user.sub)
    if row is None:
        db.add(UserAvatar(user_id=user.sub, preset=payload.preset))
    else:
        row.preset, row.image, row.content_type = payload.preset, None, None
    activity.record_event(db, user.sub, activity.PICTURE_CHANGED, ctx, session_id=activity.session_key(user, ctx))
    await db.commit()


@router.put("/avatar/image", status_code=status.HTTP_204_NO_CONTENT)
async def upload_avatar_image(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Saves a picture the user uploaded: the request body is the image itself (PNG, JPEG or WebP, at most
    AVATAR_UPLOAD_MAX_BYTES). The server scales it to a small square JPEG and keeps only that."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > AVATAR_UPLOAD_MAX_BYTES:
        raise HTTPException(413, "The image is too large")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > AVATAR_UPLOAD_MAX_BYTES:
            raise HTTPException(413, "The image is too large")
    if _image_content_type(data) is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "The image must be a PNG, JPEG or WebP")
    try:
        # Decoding and scaling take a moment of CPU: keep them off the event loop.
        jpeg = await asyncio.to_thread(make_avatar_jpeg, data)
    except AvatarImageError as exc:
        if exc.too_large:
            raise HTTPException(413, "The image has too many pixels") from exc
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "The image cannot be read") from exc

    await _ensure_user_row(db, user)
    row = await db.get(UserAvatar, user.sub)
    if row is None:
        db.add(UserAvatar(user_id=user.sub, image=jpeg, content_type="image/jpeg"))
    else:
        row.preset, row.image, row.content_type = None, jpeg, "image/jpeg"
    activity.record_event(db, user.sub, activity.PICTURE_CHANGED, ctx, session_id=activity.session_key(user, ctx))
    await db.commit()


@router.get("/avatar")
async def get_avatar_image(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The image the user uploaded (404 when the picture is a preset or nothing)."""
    row = await db.get(UserAvatar, user.sub)
    if row is None or row.image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No uploaded picture")
    return Response(
        content=row.image,
        media_type=row.content_type,
        headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def reset_avatar(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Back to the default picture."""
    row = await db.get(UserAvatar, user.sub)
    if row is not None:
        await db.delete(row)
        activity.record_event(db, user.sub, activity.PICTURE_CHANGED, ctx, session_id=activity.session_key(user, ctx))
        await db.commit()


def _token_name(user: CurrentUser) -> str:
    """The name the identity provider sent (Google, SSO), or "" after a plain registration."""
    return (user.name or "").strip()[:255]


@router.get("", response_model=UserBasicInfoOut)
async def get_user_basic_info(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
    settings: Settings = Depends(get_settings),
) -> UserBasicInfoOut:
    """The profile of the signed-in user, created on the first call from the sign-in token. The call also notes that the user
    is here (see `touch_session`): the first call of a new session records a sign-in, and `lastSeen` is when the user was last
    seen before this visit.

    The email always follows the token (it cannot be changed here). The name is taken from the token only while the
    stored name is empty, so a name the user set later is never overwritten by the next sign-in; after a plain
    registration the token has no name and the name stays empty."""
    row = await db.get(User, user.sub)
    if row is None:
        row = User(id=user.sub, email=user.email or "", name=_token_name(user))
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # A parallel first call created it a moment ago.
            await db.rollback()
            row = await db.get(User, user.sub)
            if row is None:
                raise
    else:
        changed = False
        if user.email and row.email != user.email:
            row.email = user.email
            changed = True
        if not row.name and _token_name(user):
            row.name = _token_name(user)
            changed = True
        if changed:
            await db.commit()
    last_seen = await activity.touch_session(db, user, ctx, settings)
    return UserBasicInfoOut(
        user_id=row.id,
        email=row.email,
        name=row.name,
        avatar=await _avatar_out(db, row.id),
        member_since=row.created_at,
        last_seen=last_seen,
    )


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def save_user_basic_info(
    payload: SaveUserBasicInfoIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Saves the name. The email is not part of the request: it stays the one from the sign-in token."""
    row = await db.get(User, user.sub)
    if row is None:
        row = User(id=user.sub, email=user.email or "", name=payload.name)
        db.add(row)
    else:
        if row.name != payload.name:
            activity.record_event(db, user.sub, activity.NAME_CHANGED, ctx, session_id=activity.session_key(user, ctx))
        row.name = payload.name
        if user.email:
            row.email = user.email
    await db.commit()


@router.get("/activity", response_model=UserActivityPageOut)
async def get_activity(
    limit: int = Query(20, ge=1, le=100),
    before: int | None = Query(None, ge=1, description="The `next` of the previous page"),
    kind: list[str] | None = Query(None, max_length=20, description="Only events of these kinds (repeat the parameter)"),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> UserActivityPageOut:
    """The events of the user's account, newest first, `limit` at a time. Only the user's own events are ever returned."""
    query = select(UserEvent).where(UserEvent.user_id == user.sub)
    if kind:
        query = query.where(UserEvent.kind.in_(kind))
    if before is not None:
        query = query.where(UserEvent.id < before)
    rows = (await db.execute(query.order_by(UserEvent.id.desc()).limit(limit + 1))).scalars().all()
    page, more = rows[:limit], len(rows) > limit
    current = activity.session_key(user, ctx)
    return UserActivityPageOut(
        items=[
            UserEventOut(
                id=row.id, kind=row.kind, at=row.created_at, ip=row.ip, device=row.device, details=row.details,
                session_id=row.session_id, current_session=row.session_id == current,
            )
            for row in page
        ],
        next=page[-1].id if more and page else None,
    )


@router.get("/sessions", response_model=list[UserSessionOut])
async def get_sessions(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> list[UserSessionOut]:
    """The sessions the user is signed in with: not signed out, not revoked, seen in the last 30 days."""
    since = datetime.now(timezone.utc) - activity.SESSION_LIST_WINDOW
    rows = (
        await db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == user.sub,
                UserSession.ended_at.is_(None),
                UserSession.revoked_at.is_(None),
                UserSession.last_seen_at > since,
            )
            .order_by(UserSession.last_seen_at.desc())
        )
    ).scalars().all()
    current = activity.session_key(user, ctx)
    return [
        UserSessionOut(
            id=row.id, device=row.device, mobile=row.mobile, ip=row.ip, started_at=row.started_at,
            last_seen_at=row.last_seen_at, current=row.id == current,
        )
        for row in rows
    ]


@router.delete("/sessions/current", status_code=status.HTTP_204_NO_CONTENT)
async def end_current_session(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """The user signs out on this device: the session is marked ended and a sign-out is recorded. The client calls this just
    before it signs out of Keycloak."""
    key = activity.session_key(user, ctx)
    row = await db.get(UserSession, key)
    if row is not None and row.user_id == user.sub and row.ended_at is None:
        row.ended_at = datetime.now(timezone.utc)
        activity.record_event(db, user.sub, activity.SIGN_OUT, ctx, session_id=key)
        await db.commit()


@router.delete("/sessions/others", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_other_sessions(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Signs out every session of the user but this one: the backend refuses their tokens from now on."""
    key = activity.session_key(user, ctx)
    now = datetime.now(timezone.utc)
    revoked = (
        await db.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user.sub,
                UserSession.id != key,
                UserSession.revoked_at.is_(None),
                UserSession.ended_at.is_(None),
            )
            .values(revoked_at=now)
            .returning(UserSession.id)
        )
    ).scalars().all()
    if revoked:
        activity.record_event(db, user.sub, activity.OTHER_SESSIONS_REVOKED, ctx, session_id=key, count=len(revoked))
    await db.commit()
    for session_id in revoked:
        activity.remember_revoked(session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Signs out one of the user's other sessions: the backend refuses its tokens from now on. To sign out of this device
    use DELETE /userinfo/sessions/current. A session of another user is answered as if it did not exist."""
    key = activity.session_key(user, ctx)
    row = await db.get(UserSession, session_id)
    if row is None or row.user_id != user.sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if row.id == key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This is the current session")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        activity.record_event(db, user.sub, activity.SESSION_REVOKED, ctx, session_id=key, device=row.device)
        await db.commit()
        activity.remember_revoked(row.id)
