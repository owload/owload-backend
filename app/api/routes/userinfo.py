from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser, get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.user import SaveUserBasicInfoIn, UserBasicInfoOut

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


@router.get("", response_model=UserBasicInfoOut)
async def get_user_basic_info(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserBasicInfoOut:
    row = await db.get(User, user.sub)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User profile not found")
    return UserBasicInfoOut(user_id=row.id, email=row.email, name=row.name)


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def save_user_basic_info(
    payload: SaveUserBasicInfoIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await db.get(User, user.sub)
    if row is None:
        row = User(id=user.sub, email=user.email or "", name=payload.name)
        db.add(row)
    else:
        row.name = payload.name
        if user.email:
            row.email = user.email
    await db.commit()
