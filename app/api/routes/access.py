import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_drive_and_role
from app.core.config import Settings, get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive, DriveAccessRequest, DriveInvitation, User
from app.db.session import get_db
from app.schemas.access import (
    AccessOut,
    AccessRequestOut,
    ApproveRequestIn,
    AddPersonIn,
    AddPersonOut,
    ChangeRoleIn,
    PersonOut,
    VisibilityIn,
)
from app.services import access
from app.services import activity as events
from app.services.access import PUBLIC, Role
from app.services.activity import (
    ClientContext,
    get_client_context,
    record_event,
    session_key,
)
from app.services.mailer import access_decision_mail, invitation_mail, mail_enabled, send_mail

router = APIRouter(prefix="/drives/{drive_id}/access", tags=["access"])

_ROLE_BY_NAME = {"admin": Role.ADMIN, "writer": Role.WRITER, "reader": Role.READER}


def _member_out(user_id: str, role_name: str, people: dict[str, User]) -> PersonOut:
    person = people.get(user_id)
    return PersonOut(
        id=user_id,
        status="member",
        email=person.email if person else "",
        name=(person.name or None) if person else None,
        role=access.ROLE_LABELS[access.parse_role(role_name)],  # type: ignore[arg-type]
    )


def _invitation_out(invitation: DriveInvitation) -> PersonOut:
    return PersonOut(
        id=str(invitation.id), status="invited", email=invitation.email, name=None,
        role=invitation.role, since=invitation.created_at,  # type: ignore[arg-type]
    )


async def _access_out(db: AsyncSession, drive: Drive, role: Role, settings: Settings) -> AccessOut:
    out = AccessOut(visibility=drive.visibility, my_role=access.ROLE_LABELS[role], mail_enabled=mail_enabled(settings))  # type: ignore[arg-type]
    if role < Role.ADMIN:
        return out
    if drive.visibility == PUBLIC and drive.public_token:
        out.public_link = access.public_link(settings.app_url, drive.public_token)
    if not access.has_people(drive.visibility):
        return out
    ids = list((drive.acl or {}).keys())
    users = (
        {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()} if ids else {}
    )
    out.people = [_member_out(uid, value, users) for uid, value in (drive.acl or {}).items()]
    invitations = (
        await db.execute(select(DriveInvitation).where(DriveInvitation.drive_id == drive.id).order_by(DriveInvitation.created_at))
    ).scalars().all()
    out.people += [_invitation_out(i) for i in invitations]
    return out


def _check_may_grant(actor_role: Role, target_role: Role) -> None:
    """Only the owner makes or unmakes an admin; an admin manages the writers and the readers."""
    if actor_role < Role.OWNER and target_role >= Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can add or change an admin")


@router.get("", response_model=AccessOut, response_model_by_alias=True)
async def get_access(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AccessOut:
    drive, role = await get_drive_and_role(db, drive_id, user.sub)
    return await _access_out(db, drive, role, settings)


@router.put("/visibility", response_model=AccessOut, response_model_by_alias=True)
async def set_visibility(
    drive_id: uuid.UUID,
    payload: VisibilityIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
    settings: Settings = Depends(get_settings),
) -> AccessOut:
    """Makes the drive private, shared or public. Public gives it a link (a new secret each time the drive becomes public);
    any other visibility ends the link. People belong to a shared drive and to a public one (decisions/0038): going between those two
    keeps them; making the drive private takes them all off, and cancels the invitations that have not been answered."""
    drive, role = await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    if drive.visibility != payload.visibility:
        removed = 0
        if payload.visibility == access.PRIVATE:
            removed = len(drive.acl or {}) + (
                await db.execute(delete(DriveInvitation).where(DriveInvitation.drive_id == drive.id))
            ).rowcount
            drive.acl = {}
        drive.visibility = payload.visibility
        drive.public_token = access.new_public_token() if payload.visibility == PUBLIC else None
        record_event(
            db, user.sub, events.DRIVE_VISIBILITY_CHANGED, ctx, session_id=session_key(user, ctx),
            driveId=str(drive.id), title=drive.title, visibility=payload.visibility, removedPeople=removed,
        )
        await db.commit()
    return await _access_out(db, drive, role, settings)


@router.post("/link/reset", response_model=AccessOut, response_model_by_alias=True)
async def reset_public_link(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
    settings: Settings = Depends(get_settings),
) -> AccessOut:
    """Gives a public drive a new link; the old one stops working at once."""
    drive, role = await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    if drive.visibility != PUBLIC:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The drive is not public")
    drive.public_token = access.new_public_token()
    record_event(
        db, user.sub, events.DRIVE_LINK_RESET, ctx, session_id=session_key(user, ctx), driveId=str(drive.id), title=drive.title
    )
    await db.commit()
    return await _access_out(db, drive, role, settings)


@router.post("/people", response_model=AddPersonOut, response_model_by_alias=True, status_code=status.HTTP_201_CREATED)
async def add_person(
    drive_id: uuid.UUID,
    payload: AddPersonIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
    settings: Settings = Depends(get_settings),
) -> AddPersonOut:
    """Shares the drive with an email address. Someone who already has an account with that address (verified by the identity
    provider) gets the drive at once; anyone else gets an invitation that turns into access when they sign in with it. Either
    way the person is told by mail when the backend can send mail. Only a shared or a public drive has people: for a private
    one the answer is 409."""
    drive, actor_role = await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    if not access.has_people(drive.visibility):
        raise HTTPException(status.HTTP_409_CONFLICT, "People can only be added to a shared or a public drive")
    email = access.normalize_email(payload.email)
    if not access.is_valid_email(email):
        raise HTTPException(422, "That is not an email address")
    _check_may_grant(actor_role, _ROLE_BY_NAME[payload.role])

    owner = await db.get(User, drive.owner_user_id)
    if (owner is not None and owner.email.lower() == email) or (user.sub == drive.owner_user_id and (user.email or "").lower() == email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is the owner of the drive")

    existing_user = await db.scalar(select(User).where(func.lower(User.email) == email, User.email_verified.is_(True)))
    inviter = (await db.get(User, user.sub))
    inviter_name = (inviter.name if inviter and inviter.name else None) or user.email or "Someone"
    if existing_user is not None:
        if existing_user.id == drive.owner_user_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is the owner of the drive")
        current = (drive.acl or {}).get(existing_user.id)
        if current is not None:
            _check_may_grant(actor_role, access.parse_role(current))  # an admin does not change another admin
        drive.acl = {**(drive.acl or {}), existing_user.id: payload.role}
        await db.execute(  # an invitation to the same address is now redundant
            DriveInvitation.__table__.delete().where(DriveInvitation.drive_id == drive.id, DriveInvitation.email == email)
        )
        person = _member_out(existing_user.id, payload.role, {existing_user.id: existing_user})
        kind = events.DRIVE_PERSON_ADDED
    else:
        invitation = await db.scalar(
            select(DriveInvitation).where(DriveInvitation.drive_id == drive.id, DriveInvitation.email == email)
        )
        if invitation is None:
            invitation = DriveInvitation(
                id=access.new_id(), drive_id=drive.id, email=email, role=payload.role, invited_by=user.sub
            )
            db.add(invitation)
        else:
            invitation.role = payload.role
        await db.flush()
        person = _invitation_out(invitation)
        kind = events.DRIVE_PERSON_INVITED
    record_event(
        db, user.sub, kind, ctx, session_id=session_key(user, ctx),
        driveId=str(drive.id), title=drive.title, email=email, role=payload.role,
    )
    await db.commit()

    subject, body, html_body = invitation_mail(inviter_name, drive.title, payload.role, access.drive_link(settings.app_url, drive.id), existing_user is not None, email)
    email_sent = await send_mail(settings, email, subject, body, html_body)
    return AddPersonOut(person=person, email_sent=email_sent, app_url=settings.app_url)  # type: ignore[arg-type]


@router.get("/requests", response_model=list[AccessRequestOut], response_model_by_alias=True)
async def list_requests(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AccessRequestOut]:
    """The pending requests of people who asked to be let into the drive (decisions/0037), oldest first. The owner and the admins."""
    await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    rows = (
        await db.execute(
            select(DriveAccessRequest, User)
            .join(User, User.id == DriveAccessRequest.user_id, isouter=True)
            .where(DriveAccessRequest.drive_id == drive_id)
            .order_by(DriveAccessRequest.created_at)
        )
    ).all()
    return [
        AccessRequestOut(
            user_id=request.user_id, name=(person.name or None) if person else None, email=person.email if person else "",
            message=request.message, requested_at=request.created_at,
        )
        for request, person in rows
    ]


@router.post("/requests/{person_id}/approve", response_model=AddPersonOut, response_model_by_alias=True)
async def approve_request(
    drive_id: uuid.UUID,
    person_id: str,
    payload: ApproveRequestIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
    settings: Settings = Depends(get_settings),
) -> AddPersonOut:
    """Lets the person in with the chosen role and tells them (`emailSent` says whether the mail went out). Only a drive that is shared or public has people: for a private
    one the answer is 409 (asking never changes who can see a drive). Only the owner makes an admin."""
    drive, actor_role = await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    request = await db.scalar(
        select(DriveAccessRequest).where(DriveAccessRequest.drive_id == drive_id, DriveAccessRequest.user_id == person_id)
    )
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    if not access.has_people(drive.visibility):
        raise HTTPException(status.HTTP_409_CONFLICT, "Share the drive with people first: a private drive has no people")
    _check_may_grant(actor_role, _ROLE_BY_NAME[payload.role])
    person = await db.get(User, person_id)
    if person is None or person_id == drive.owner_user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")

    drive.acl = {**(drive.acl or {}), person_id: payload.role}
    await db.delete(request)
    record_event(
        db, user.sub, events.DRIVE_PERSON_ADDED, ctx, session_id=session_key(user, ctx),
        driveId=str(drive.id), title=drive.title, email=person.email, role=payload.role,
    )
    await db.commit()

    subject, body, html_body = access_decision_mail(drive.title, True, payload.role, access.drive_link(settings.app_url, drive.id), person.email)
    email_sent = await send_mail(settings, person.email, subject, body, html_body)
    return AddPersonOut(person=_member_out(person_id, payload.role, {person_id: person}), email_sent=email_sent, app_url=settings.app_url)  # type: ignore[arg-type]


@router.delete("/requests/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def decline_request(
    drive_id: uuid.UUID,
    person_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Declines a request and tells the person. Always possible, whatever the visibility of the drive."""
    drive, _ = await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    request = await db.scalar(
        select(DriveAccessRequest).where(DriveAccessRequest.drive_id == drive_id, DriveAccessRequest.user_id == person_id)
    )
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    person = await db.get(User, person_id)
    await db.delete(request)
    await db.commit()
    if person is not None:
        subject, body, html_body = access_decision_mail(drive.title, False, "", settings.app_url, person.email)
        await send_mail(settings, person.email, subject, body, html_body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/people/{person_id}", response_model=PersonOut)
async def change_role(
    drive_id: uuid.UUID,
    person_id: str,
    payload: ChangeRoleIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> PersonOut:
    drive, actor_role = await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    new_role = _ROLE_BY_NAME[payload.role]
    _check_may_grant(actor_role, new_role)
    invitation = await _find_invitation(db, drive.id, person_id)
    if invitation is not None:
        _check_may_grant(actor_role, _ROLE_BY_NAME[invitation.role])
        invitation.role = payload.role
        out = _invitation_out(invitation)
        email = invitation.email
    elif person_id in (drive.acl or {}):
        _check_may_grant(actor_role, access.parse_role(drive.acl[person_id]))
        drive.acl = {**drive.acl, person_id: payload.role}
        member = await db.get(User, person_id)
        out = _member_out(person_id, payload.role, {person_id: member} if member else {})
        email = member.email if member else ""
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    record_event(
        db, user.sub, events.DRIVE_ROLE_CHANGED, ctx, session_id=session_key(user, ctx),
        driveId=str(drive.id), title=drive.title, email=email, role=payload.role,
    )
    await db.commit()
    return out


@router.delete("/people/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_person(
    drive_id: uuid.UUID,
    person_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> Response:
    drive, actor_role = await get_drive_and_role(db, drive_id, user.sub, Role.ADMIN)
    invitation = await _find_invitation(db, drive.id, person_id)
    if invitation is not None:
        _check_may_grant(actor_role, _ROLE_BY_NAME[invitation.role])
        email = invitation.email
        await db.delete(invitation)
    elif person_id in (drive.acl or {}):
        _check_may_grant(actor_role, access.parse_role(drive.acl[person_id]))
        member = await db.get(User, person_id)
        email = member.email if member else ""
        drive.acl = {k: v for k, v in drive.acl.items() if k != person_id}
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    record_event(
        db, user.sub, events.DRIVE_PERSON_REMOVED, ctx, session_id=session_key(user, ctx),
        driveId=str(drive.id), title=drive.title, email=email,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def leave_drive(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> Response:
    """Someone the drive was shared with gives it up. The owner cannot: they delete the drive."""
    drive, role = await get_drive_and_role(db, drive_id, user.sub)
    if role == Role.OWNER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The owner cannot leave the drive; delete it instead")
    drive.acl = {k: v for k, v in (drive.acl or {}).items() if k != user.sub}
    record_event(db, user.sub, events.DRIVE_LEFT, ctx, session_id=session_key(user, ctx), driveId=str(drive.id), title=drive.title)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _find_invitation(db: AsyncSession, drive_id: uuid.UUID, person_id: str) -> DriveInvitation | None:
    try:
        invitation_id = uuid.UUID(person_id)
    except ValueError:
        return None
    invitation = await db.get(DriveInvitation, invitation_id)
    return invitation if invitation is not None and invitation.drive_id == drive_id else None
