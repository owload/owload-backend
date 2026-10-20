"""Who may do what on a drive: visibility, roles, the public link and the invitations by email.

A drive is `private` (only the owner), `shared` (the owner and the people in its ACL) or `public` (the same, and anyone with
the link can read it, without signing in). The ACL maps a user id to a role: `reader` views, `writer` also adds and changes
files, `admin` also manages people and settings. The owner can do everything, including deleting the drive. The content of a
drive is encrypted with a password the server never sees, so a person who is let in still has to be told the password."""
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from enum import IntEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUser
from app.db.models import Drive, DriveInvitation

PRIVATE = "private"
SHARED = "shared"
PUBLIC = "public"
VISIBILITIES = (PRIVATE, SHARED, PUBLIC)


def has_people(visibility: str) -> bool:
    """Whether a drive of this visibility has people (an ACL): a shared one and a public one; only a private drive is the owner's alone
    (decisions/0038)."""
    return visibility in (SHARED, PUBLIC)

READER = "reader"
WRITER = "writer"
ADMIN = "admin"
ROLES = (ADMIN, WRITER, READER)


class Role(IntEnum):
    """The ranks of the people on a drive; compare with `>=`."""

    READER = 1
    WRITER = 2
    ADMIN = 3
    OWNER = 4


_ROLE_BY_NAME = {READER: Role.READER, WRITER: Role.WRITER, ADMIN: Role.ADMIN}
ROLE_LABELS = {Role.READER: "reader", Role.WRITER: "writer", Role.ADMIN: "admin", Role.OWNER: "owner"}


def parse_role(value: object) -> Role:
    """The role in an ACL entry. Older rows hold a list of privileges ("READ", "WRITE") instead of the name of a role."""
    if isinstance(value, str):
        return _ROLE_BY_NAME.get(value, Role.READER)
    if isinstance(value, list):
        return Role.WRITER if "WRITE" in value else Role.READER
    return Role.READER


def role_of(drive: Drive, user_id: str) -> Role | None:
    """The role the user has on the drive, or None when the drive is not theirs to open: the owner always has the drive; the
    people in the ACL have it while the drive is shared or public (an ACL left on a private drive counts for nothing, decisions/0038)."""
    if drive.owner_user_id == user_id:
        return Role.OWNER
    if not has_people(drive.visibility) or user_id not in (drive.acl or {}):
        return None
    return parse_role(drive.acl[user_id])


def normalize_email(email: str) -> str:
    return email.strip().lower()


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return len(email) <= 320 and bool(_EMAIL_PATTERN.match(email))


def new_public_token() -> str:
    """The secret of a public link: 32 random bytes, in the form fit for a URL."""
    return secrets.token_urlsafe(32)


def drive_link(app_url: str, drive_id: uuid.UUID | str) -> str:
    """The address that opens a drive in the app (after signing in; a person who is not on it is offered to ask for access)."""
    return f"{app_url.rstrip('/')}/drive/{drive_id}"


def public_link(app_url: str, token: str) -> str:
    return f"{app_url.rstrip('/')}/p/{token}"


async def accept_invitations(db: AsyncSession, user: CurrentUser) -> list[tuple[Drive, str]]:
    """Gives the drives shared with the user's email to the user, when the identity provider vouches for that email. Returns
    the drives and the roles that were given (the caller records the events and commits)."""
    if not user.email or not user.email_verified:
        return []
    invitations = (
        await db.execute(select(DriveInvitation).where(DriveInvitation.email == normalize_email(user.email)))
    ).scalars().all()
    accepted: list[tuple[Drive, str]] = []
    for invitation in invitations:
        drive = await db.get(Drive, invitation.drive_id)
        if drive is not None and has_people(drive.visibility) and drive.owner_user_id != user.sub:
            drive.acl = {**(drive.acl or {}), user.sub: invitation.role}
            accepted.append((drive, invitation.role))
        await db.delete(invitation)
    return accepted


_recent_public_reads: dict[str, list[float]] = {}


def public_read_allowed(client_ip: str | None, limit_per_minute: int) -> bool:
    """A budget of reads of the public drives per IP address and minute (kept in memory, per process): a public link is the only
    secret of a public drive, so guessing it must be slow, and an open drive must not be a way to drain the storage."""
    if limit_per_minute <= 0:
        return True
    key = client_ip or "unknown"
    now = time.monotonic()
    recent = [t for t in _recent_public_reads.get(key, []) if now - t < 60.0]
    if len(recent) >= limit_per_minute:
        _recent_public_reads[key] = recent
        return False
    recent.append(now)
    _recent_public_reads[key] = recent
    if len(_recent_public_reads) > 10_000:
        _recent_public_reads.clear()
    return True


_recent_access_requests: dict[str, list[float]] = {}
ACCESS_REQUESTS_PER_HOUR = 5


def access_request_allowed(user_id: str, per_hour: int = ACCESS_REQUESTS_PER_HOUR) -> bool:
    """A budget of requests for access per person and hour (kept in memory, per process): a request mails the admins of somebody else's
    drive, so it must not be a way to fill their inbox (decisions/0037)."""
    now = time.monotonic()
    recent = [t for t in _recent_access_requests.get(user_id, []) if now - t < 3600.0]
    if len(recent) >= per_hour:
        _recent_access_requests[user_id] = recent
        return False
    recent.append(now)
    _recent_access_requests[user_id] = recent
    if len(_recent_access_requests) > 10_000:
        _recent_access_requests.clear()
    return True


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> uuid.UUID:
    return uuid.uuid4()
