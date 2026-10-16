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
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.db.models import Drive, UserEvent, UserSession

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
# Access to a drive, squashed into bursts (see note_drive_activity).
DRIVE_OPENED = "drive_opened"
DRIVE_FILES_READ = "drive_files_read"
DRIVE_WRITTEN = "drive_written"
# Who may open a drive.
DRIVE_VISIBILITY_CHANGED = "drive_visibility_changed"
DRIVE_LINK_RESET = "drive_link_reset"
DRIVE_PERSON_ADDED = "drive_person_added"
DRIVE_PERSON_INVITED = "drive_person_invited"
DRIVE_PERSON_REMOVED = "drive_person_removed"
DRIVE_ROLE_CHANGED = "drive_role_changed"
DRIVE_SHARED_WITH_YOU = "drive_shared_with_you"
DRIVE_LEFT = "drive_left"
STORAGE_TARGET_ADDED = "storage_target_added"
STORAGE_TARGET_MAIN_CHANGED = "storage_target_main_changed"
STORAGE_TARGET_REMOVED = "storage_target_removed"

UNKNOWN_DEVICE = "Unknown device"
SEEN_WRITE_INTERVAL = timedelta(seconds=60)
NEW_VISIT_GAP = timedelta(minutes=30)
# A session that has not been seen for this long is no longer listed as signed in.
SESSION_LIST_WINDOW = timedelta(days=30)
SESSION_KEEP = timedelta(days=90)
# Drive access is recorded once per burst: while a user keeps reading or writing the same drive from the same session with gaps
# shorter than BURST_GAP it is one event, whose `lastAt` moves on. Between two looks at the database there are at least
# DRIVE_NOTE_INTERVAL seconds, so a stream of requests (the ranges of a video, the blocks of an upload) costs nothing.
BURST_GAP = timedelta(minutes=15)
DRIVE_NOTE_INTERVAL = 30.0


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
        return UNKNOWN_DEVICE, False
    browser = next((name for name, pattern in _BROWSERS if pattern.search(user_agent)), None)
    system = next((name for name, pattern in _SYSTEMS if pattern.search(user_agent)), None)
    mobile = bool(re.search(r"\b(iPhone|iPod|Android.+Mobile|Mobile)\b", user_agent))
    if browser and system:
        return f"{browser} on {system}", mobile
    return browser or system or UNKNOWN_DEVICE, mobile


def get_client_context(request: Request, settings: Settings = Depends(get_settings)) -> ClientContext:
    user_agent = (request.headers.get("user-agent") or "").strip()[:512] or None
    device, mobile = describe_device(user_agent)
    return ClientContext(ip=_client_ip(request, settings), user_agent=user_agent, device=device, mobile=mobile)


def session_key(user: CurrentUser, ctx: ClientContext) -> str:
    """The id of the user's session: the Keycloak `sid`; for a token without one, a stand-in made of the user and the browser."""
    if user.session_id:
        return user.session_id
    return hashlib.sha256(f"{user.sub}|{ctx.user_agent or ''}".encode()).hexdigest()[:32]


def collapse_sessions(rows: list[UserSession], current_id: str | None) -> list[UserSession]:
    """One row for each device. Every sign-in has a session of its own, so the same browser on the same system from the same address that
    has signed in again (a session that ran out, a closed window, a private window) leaves older sessions behind: they are the same device
    and are not listed again. A device is told by its description ("Chrome on macOS", not the full browser string, whose version changes with
    every update) and its address. The row kept is the current session if the device has it, else the one seen last. `rows` come with the
    latest first."""
    chosen: dict[tuple, UserSession] = {}
    for row in rows:
        key = (row.device, row.ip) if row.device != UNKNOWN_DEVICE else (row.id,)  # an unknown device is not guessed to be another's
        kept = chosen.get(key)
        if kept is None or (row.id == current_id and kept.id != current_id):
            chosen[key] = row
    return list(chosen.values())


def same_device(user_id: str, session: UserSession):
    """The condition for the sessions of the same device as `session` (see `collapse_sessions`), itself included."""
    return and_(
        UserSession.user_id == user_id,
        UserSession.device == session.device if session.device != UNKNOWN_DEVICE else UserSession.id == session.id,
        UserSession.ip.is_not_distinct_from(session.ip),
    )


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


_recent_drive_notes: dict[tuple[str, str, str], float] = {}


DRIVE_READ_TOUCH_INTERVAL = 60.0
_recent_drive_reads: dict[str, float] = {}


async def note_drive_read(db: AsyncSession, drive_id) -> None:
    """Records that the drive was read just now, by anyone: it is what the drive's card shows as "last read". Written at most once a
    minute per drive (kept in memory, per process), and committed by itself because a read route has nothing else to commit."""
    key = str(drive_id)
    moment = time.monotonic()
    last = _recent_drive_reads.get(key)
    if last is not None and moment - last < DRIVE_READ_TOUCH_INTERVAL:
        return
    if len(_recent_drive_reads) > 10_000:
        _recent_drive_reads.clear()
    _recent_drive_reads[key] = moment
    await db.execute(update(Drive).where(Drive.id == drive_id).values(last_read_at=func.now()))
    await db.commit()


async def note_drive_activity(
    db: AsyncSession, user: CurrentUser, drive_id, title: str, kind: str, ctx: ClientContext, *, commit: bool = False
) -> None:
    """Notes that the user read or wrote the drive (`kind`: opened, read files, wrote), without saying what: one event per burst of
    such requests from one session. Nothing is sent by the client: the backend sees the requests it serves anyway.

    A write route stages the event in its own transaction and commits as it always did; a read route has nothing else to commit,
    so it passes `commit=True`."""
    key = (user.sub, str(drive_id), kind)
    moment = time.monotonic()
    last_look = _recent_drive_notes.get(key)
    if last_look is not None and moment - last_look < DRIVE_NOTE_INTERVAL:
        return
    if len(_recent_drive_notes) > 10_000:
        _recent_drive_notes.clear()
    _recent_drive_notes[key] = moment

    now = datetime.now(timezone.utc)
    session_id = session_key(user, ctx)
    latest = await db.scalar(
        select(UserEvent)
        .where(
            UserEvent.user_id == user.sub,
            UserEvent.kind == kind,
            UserEvent.session_id == session_id,
            UserEvent.details["driveId"].astext == str(drive_id),
        )
        .order_by(UserEvent.id.desc())
        .limit(1)
    )
    last_at = None
    if latest is not None:
        try:
            last_at = datetime.fromisoformat(str(latest.details.get("lastAt")))
        except ValueError:
            last_at = None
    if latest is not None and last_at is not None and now - last_at < BURST_GAP:
        latest.details = {**latest.details, "lastAt": now.isoformat(), "title": title}
    else:
        record_event(db, user.sub, kind, ctx, session_id=session_id, driveId=str(drive_id), title=title, lastAt=now.isoformat())
    if commit:
        await db.commit()


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
