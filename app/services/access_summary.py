"""What the card of a drive shows about who is on it and who read it (owload-docs/decisions/0039): made from the drive's ACL, the profiles of the
people on it and the "opened" and "read files" events the server already keeps; nothing new is stored. Names only: a person on a drive sees the names of the
others, never their addresses, and who read the drive and when (their own reads among them); the owner and the admins also see the roles of the
others as counts and the public link."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Drive, User, UserEvent
from app.services import access
from app.services.access import Role
from app.services.activity import DRIVE_FILES_READ, DRIVE_OPENED

MAX_PEOPLE_SHOWN = 5
MAX_READS_SHOWN = 12
READS_LOOK_BACK = timedelta(days=30)
# how many of the latest "opened" and "read files" events of the drives are looked at (the events of several drives are asked for in one go)
READ_EVENTS_LIMIT = 800


@dataclass
class Person:
    id: str
    name: str
    is_me: bool
    role: str


@dataclass
class Read:
    user_id: str
    name: str
    at: datetime
    role: str
    kind: str  # "opened": the drive was opened (its operations read); "read": the data of its files was read


@dataclass
class Summary:
    people: list[Person] = field(default_factory=list)
    total: int = 1
    roles: dict[str, int] = field(default_factory=dict)
    public_link: str | None = None
    recent_reads: list[Read] = field(default_factory=list)


def _role_name(value: object) -> str:
    return access.ROLE_LABELS[access.parse_role(value)]


def _display_name(person: User | None, sees_addresses: bool) -> str:
    if person is None:
        return "Member"
    if person.name:
        return person.name
    return person.email if sees_addresses and person.email else "Member"


async def summaries(db: AsyncSession, entries: list[tuple[Drive, Role]], viewer_id: str, settings: Settings) -> dict[uuid.UUID, Summary]:
    """The summary of each drive for the person `viewer_id`, who has the role given with it."""
    wanted: set[str] = set()
    for drive, _ in entries:
        wanted.add(drive.owner_user_id)
        if access.has_people(drive.visibility):
            wanted.update((drive.acl or {}).keys())
    profiles = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(wanted)))).scalars().all()} if wanted else {}

    everywhere = [str(drive.id) for drive, _ in entries]
    last_reads: dict[tuple[str, str], tuple[datetime, str]] = {}
    if everywhere:
        since = datetime.now(timezone.utc) - READS_LOOK_BACK
        rows = (
            await db.execute(
                select(UserEvent.user_id, UserEvent.kind, UserEvent.details, UserEvent.created_at)
                .where(UserEvent.kind.in_([DRIVE_FILES_READ, DRIVE_OPENED]), UserEvent.details["driveId"].astext.in_(everywhere), UserEvent.created_at > since)
                .order_by(UserEvent.id.desc())
                .limit(READ_EVENTS_LIMIT)
            )
        ).all()
        for user_id, event_kind, details, created_at in rows:
            key = (str(details.get("driveId")), user_id)
            try:
                at = datetime.fromisoformat(str(details.get("lastAt")))
            except ValueError:
                at = created_at
            if key not in last_reads or at > last_reads[key][0]:  # the latest of the two kinds: a burst of reads goes on after its first event
                last_reads[key] = (at, "read" if event_kind == DRIVE_FILES_READ else "opened")

    result: dict[uuid.UUID, Summary] = {}
    for drive, role in entries:
        sees_addresses = role >= Role.ADMIN
        members: list[tuple[str, str]] = [(drive.owner_user_id, "owner")]
        if access.has_people(drive.visibility):
            members += [(uid, _role_name(value)) for uid, value in (drive.acl or {}).items() if uid != drive.owner_user_id]
        # the viewer is shown first when they are on it, then the owner, then the rest in the order they were added
        ordered = sorted(members, key=lambda m: (m[0] != viewer_id, m[1] != "owner"))
        summary = Summary(total=len(members))
        summary.people = [
            Person(uid, "You" if uid == viewer_id else _display_name(profiles.get(uid), sees_addresses), uid == viewer_id, member_role)
            for uid, member_role in ordered[:MAX_PEOPLE_SHOWN]
        ]
        if sees_addresses:
            for _, member_role in members:
                if member_role != "owner":
                    summary.roles[member_role] = summary.roles.get(member_role, 0) + 1
            if drive.visibility == access.PUBLIC and drive.public_token:
                summary.public_link = access.public_link(settings.app_url, drive.public_token)
        # everyone on the drive sees who read it (by name), themselves included
        member_ids = {uid for uid, _ in members}
        role_by_member = dict(members)
        reads = [
            Read(uid, "You" if uid == viewer_id else _display_name(profiles.get(uid), True), at, role_by_member[uid], how)
            for (drive_id, uid), (at, how) in last_reads.items()
            if drive_id == str(drive.id) and uid in member_ids
        ]
        summary.recent_reads = sorted(reads, key=lambda r: r.at, reverse=True)[:MAX_READS_SHOWN]
        result[drive.id] = summary
    return result
