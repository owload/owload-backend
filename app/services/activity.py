"""Account activity: where a request came from, the user's sessions and the events of their account.

The backend sees a sign-in only as the first request of a new Keycloak session (the `sid` of the token), so a "sign-in"
here is the first time a session is seen. The IP address and the browser of the request are kept with it, for the user to
see where their account is used from; they are personal data, kept for `activity_retention_days` and shown only to the
user they belong to."""
import hashlib
import ipaddress
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.db.models import UserEvent, UserSession

# Event kinds.
SIGN_IN = "sign_in"
SIGN_OUT = "sign_out"
SESSION_REVOKED = "session_revoked"
OTHER_SESSIONS_REVOKED = "other_sessions_revoked"
NAME_CHANGED = "name_changed"
PICTURE_CHANGED = "picture_changed"
DRIVE_CREATED = "drive_created"
DRIVE_RESTORED = "drive_restored"
DRIVE_DELETED = "drive_deleted"

SEEN_WRITE_INTERVAL = timedelta(seconds=60)
NEW_VISIT_GAP = timedelta(minutes=30)
# A session that has not been seen for this long is no longer listed as signed in.
SESSION_LIST_WINDOW = timedelta(days=30)
SESSION_KEEP = timedelta(days=90)


@dataclass(frozen=True)
class ClientContext:
    ip: str | None
    user_agent: str | None
    device: str
    mobile: bool


def _client_ip(request: Request, settings: Settings) -> str | None:
    candidate = request.client.host if request.client else None
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if len(forwarded) >= hops:
            candidate = forwarded[-hops]
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else None
    except ValueError:
        return None


_BROWSERS = (
    ("Edge", re.compile(r"\b(Edg|EdgA|EdgiOS)/")),
    ("Opera", re.compile(r"\bOPR/|\bOpera\b")),
    ("Yandex Browser", re.compile(r"\bYaBrowser/")),
    ("Firefox", re.compile(r"\b(Firefox|FxiOS)/")),
    ("Chrome", re.compile(r"\b(Chrome|CriOS)/")),
    ("Safari", re.compile(r"\bSafari/")),
)
_SYSTEMS = (
    ("iOS", re.compile(r"\b(iPhone|iPod)\b")),
    ("iPadOS", re.compile(r"\biPad\b")),
    ("Android", re.compile(r"\bAndroid\b")),
    ("Windows", re.compile(r"\bWindows\b")),
    ("macOS", re.compile(r"\bMac OS X\b|\bMacintosh\b")),
    ("ChromeOS", re.compile(r"\bCrOS\b")),
    ("Linux", re.compile(r"\bLinux\b|\bX11\b")),
)


def describe_device(user_agent: str | None) -> tuple[str, bool]:
    """A short label for a browser ("Chrome on macOS") and whether it is a phone, from its User-Agent."""
    if not user_agent:
        return "Unknown device", False
    browser = next((name for name, pattern in _BROWSERS if pattern.search(user_agent)), None)
    system = next((name for name, pattern in _SYSTEMS if pattern.search(user_agent)), None)
    mobile = bool(re.search(r"\b(iPhone|iPod|Android.+Mobile|Mobile)\b", user_agent))
    if browser and system:
        return f"{browser} on {system}", mobile
    return browser or system or "Unknown device", mobile


def get_client_context(request: Request, settings: Settings = Depends(get_settings)) -> ClientContext:
    user_agent = (request.headers.get("user-agent") or "").strip()[:512] or None
    device, mobile = describe_device(user_agent)
    return ClientContext(ip=_client_ip(request, settings), user_agent=user_agent, device=device, mobile=mobile)


def session_key(user: CurrentUser, ctx: ClientContext) -> str:
    """The id of the user's session: the Keycloak `sid`; for a token without one, a stand-in made of the user and the browser."""
    if user.session_id:
        return user.session_id
    return hashlib.sha256(f"{user.sub}|{ctx.user_agent or ''}".encode()).hexdigest()[:32]


def record_event(db: AsyncSession, user_id: str, kind: str, ctx: ClientContext, session_id: str | None = None, **details) -> None:
    """Stages an event in the caller's transaction (it never commits on its own), so it persists only if the action it
    describes does."""
    db.add(UserEvent(user_id=user_id, kind=kind, session_id=session_id, ip=ctx.ip, device=ctx.device, details=details))


async def prune_old_activity(db: AsyncSession, user_id: str, settings: Settings) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        delete(UserEvent).where(UserEvent.user_id == user_id, UserEvent.created_at < now - timedelta(days=settings.activity_retention_days))
    )
    await db.execute(delete(UserSession).where(UserSession.user_id == user_id, UserSession.last_seen_at < now - SESSION_KEEP))


async def touch_session(db: AsyncSession, user: CurrentUser, ctx: ClientContext, settings: Settings) -> datetime | None:
    """Notes that the user is here with this session. A new session records a sign-in. Returns when the user was last seen
    before this visit (None for the very first), which stays the same for all the calls of one visit."""
    key = session_key(user, ctx)
    now = datetime.now(timezone.utc)
    row = await db.get(UserSession, key)
    if row is None:
        previous = await db.scalar(select(func.max(UserSession.last_seen_at)).where(UserSession.user_id == user.sub))
        inserted = await db.execute(
            pg_insert(UserSession)
            .values(
                id=key, user_id=user.sub, ip=ctx.ip, user_agent=ctx.user_agent, device=ctx.device, mobile=ctx.mobile,
                started_at=now, last_seen_at=now, prev_seen_at=previous,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        # Two calls of the same first visit can race: only the one that inserted the row records the sign-in.
        if inserted.rowcount == 1:
            record_event(db, user.sub, SIGN_IN, ctx, session_id=key)
            await prune_old_activity(db, user.sub, settings)
        await db.commit()
        row = await db.get(UserSession, key)
        return row.prev_seen_at if row else previous

    gap = now - row.last_seen_at
    changed = False
    if row.ended_at is not None:
        row.ended_at = None
        changed = True
    if gap > NEW_VISIT_GAP:
        row.prev_seen_at = row.last_seen_at
        changed = True
    if gap > SEEN_WRITE_INTERVAL or (ctx.ip and ctx.ip != row.ip):
        row.last_seen_at = now
        row.ip, row.user_agent, row.device, row.mobile = ctx.ip or row.ip, ctx.user_agent or row.user_agent, ctx.device, ctx.mobile
        changed = True
    if changed:
        await db.commit()
    return row.prev_seen_at


_revoked_cache: dict[str, tuple[float, bool]] = {}
_REVOKED_CACHE_SECONDS = 10.0


def remember_revoked(session_id: str) -> None:
    _revoked_cache[session_id] = (time.monotonic(), True)


async def is_session_revoked(db: AsyncSession, session_id: str) -> bool:
    """Whether the user signed this session out from another device. Looked up at most once in a few seconds per session."""
    cached = _revoked_cache.get(session_id)
    if cached and time.monotonic() - cached[0] < _REVOKED_CACHE_SECONDS:
        return cached[1]
    revoked = await db.scalar(select(UserSession.revoked_at).where(UserSession.id == session_id)) is not None
    if len(_revoked_cache) > 10_000:
        _revoked_cache.clear()
    _revoked_cache[session_id] = (time.monotonic(), revoked)
    return revoked
