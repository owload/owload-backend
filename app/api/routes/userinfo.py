import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, get_current_user
from app.db.models import User, UserAvatar
from app.db.session import get_db
from app.services.avatar_image import AvatarImageError, make_avatar_jpeg
from app.schemas.user import (
    AVATAR_UPLOAD_MAX_BYTES,
    AvatarOut,
    SaveUserBasicInfoIn,
    SetAvatarPresetIn,
    UserBasicInfoOut,
)

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
) -> None:
    """Chooses one of the preset pictures (this drops an uploaded image)."""
    await _ensure_user_row(db, user)
    row = await db.get(UserAvatar, user.sub)
    if row is None:
        db.add(UserAvatar(user_id=user.sub, preset=payload.preset))
    else:
        row.preset, row.image, row.content_type = payload.preset, None, None
    await db.commit()


@router.put("/avatar/image", status_code=status.HTTP_204_NO_CONTENT)
async def upload_avatar_image(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
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
) -> None:
    """Back to the default picture."""
    row = await db.get(UserAvatar, user.sub)
    if row is not None:
        await db.delete(row)
        await db.commit()


def _token_name(user: CurrentUser) -> str:
    """The name the identity provider sent (Google, SSO), or "" after a plain registration."""
    return (user.name or "").strip()[:255]


@router.get("", response_model=UserBasicInfoOut)
async def get_user_basic_info(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBasicInfoOut:
    """The profile of the signed-in user, created on the first call from the sign-in token.

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
    return UserBasicInfoOut(
        user_id=row.id,
        email=row.email,
        name=row.name,
        avatar=await _avatar_out(db, row.id),
        member_since=row.created_at,
    )


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def save_user_basic_info(
    payload: SaveUserBasicInfoIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Saves the name. The email is not part of the request: it stays the one from the sign-in token."""
    row = await db.get(User, user.sub)
    if row is None:
        row = User(id=user.sub, email=user.email or "", name=payload.name)
        db.add(row)
    else:
        row.name = payload.name
        if user.email:
            row.email = user.email
    await db.commit()
