import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    """Local profile for a Keycloak identity. Row is created lazily on first
    POST /userinfo — the JWT alone (sub/email) is not enough to support
    findUsers() search, so we mirror a minimal profile here."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Keycloak `sub`
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserAvatar(Base):
    """The picture of a user's profile: one of the preset pictures the client draws (`preset`), or a small image the user
    uploaded (`image`, always a small square JPEG made by the server). A user who chose nothing has no row and the
    client shows the initials of the name. Kept apart from `users` so the user search never loads the image bytes."""

    __tablename__ = "user_avatars"
    __table_args__ = (
        CheckConstraint("(preset IS NULL) <> (image IS NULL)", name="ck_user_avatars_preset_xor_image"),
    )

    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserSession(Base):
    """A sign-in of a user to Owload, seen by the backend: the Keycloak session id (`sid`) with where it came from. Created the
    first time a token of that session is seen; `revoked_at` is set when the user signs the session out from another device
    (the backend then refuses its tokens, see app/api/deps.py)."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    device: Mapped[str] = mapped_column(String(120), nullable=False, default="Unknown device")
    mobile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the user was last seen before the current visit (the visit being a run of activity without a 30 minute gap).
    prev_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserEvent(Base):
    """What the user did to their account and from where: sign-ins and sign-outs, profile changes, drives created or deleted.
    Written in the same transaction as the action it describes (see app/services/activity.py). Not a log of file operations:
    those are in `drive_action_log`."""

    __tablename__ = "user_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    device: Mapped[str | None] = mapped_column(String(120), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Drive(Base):
    """An encrypted drive: an append-only ops log plus a byte-addressable
    data area. Both are opaque ciphertext to the server — encryption/decryption
    and the filesystem structure they encode live entirely on the client.
    """

    __tablename__ = "drives"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    # dict[userId, list[str]] of Privilege names ("READ"/"WRITE") shared with other users.
    acl: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    key_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    counter_nonce: Mapped[str] = mapped_column(Text, nullable=False)

    # Append-only operation log, opaque bytes. Grown via atomic `ops_data || :chunk` updates.
    ops_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")

    # Total length of the byte-addressable data area (see UploadSession below).
    data_length: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionStatus:
    ACTIVE = 0
    FINISHED = 1
    STALE = 2


class UploadSession(Base):
    """Reserves a [byte_offset, byte_offset + byte_length) region in a
    drive's data area. saveDataBlock writes land at byte_offset +
    blockByteOffset; finalize just flips the status flag, matching the
    frontend's own in-memory reference implementation.
    """

    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drives.id", ondelete="CASCADE"), nullable=False, index=True
    )
    byte_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_length: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=SessionStatus.ACTIVE)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_write_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    drive: Mapped[Drive] = relationship()


class DriveAction:
    """GET_OPS/GET_DATA/SAVE_DATA are deliberately absent: they don't
    correspond 1:1 to a meaningful user action (a single read/write chunking
    decision made by the client transport), unlike the actions below, which
    are each exactly one row per one semantic action — see
    owload-docs/decisions/0003 and owload-docs/decisions/0008."""

    CREATE_DRIVE = "CREATE_DRIVE"
    RESTORE_DRIVE = "RESTORE_DRIVE"
    START_SESSION = "START_SESSION"
    FINALIZE_SESSION = "FINALIZE_SESSION"
    SAVE_OP = "SAVE_OP"
    DELETE_DATA = "DELETE_DATA"


class DriveActionLog(Base):
    """Audit trail of user actions on a drive. Written only on success — the
    row is added to the same DB transaction as the action's own commit, so it
    never persists if the action itself didn't (see app/services/audit.py)."""

    __tablename__ = "drive_action_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drives.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Action-specific extra fields (e.g. {"start": ..., "end": ...} for byte-range actions).
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Monotonic keyset-pagination cursor — sequence-backed (see migration),
    # not a plain identity column, because existing rows are backfilled in
    # (created_at, id) order rather than physical/heap order. Global across
    # drives, but still strictly ordered within any single drive_id filter.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, server_default=FetchedValue())


class S3Tier:
    HOT = "HOT"
    COLD = "COLD"


class StorageTargetRole:
    MASTER = "MASTER"
    SLAVE = "SLAVE"


class StorageTargetStatus:
    ACTIVE = "ACTIVE"
    PROVISIONING = "PROVISIONING"
    REMOVING = "REMOVING"


class S3Preset(Base):
    """Admin-curated pool of reusable S3 connection targets (`tier` HOT/COLD).
    No CRUD/UI for this table — rows are seeded directly via migration/config,
    see owload-docs/epics/0012-multi-s3-storage-topology."""

    __tablename__ = "s3_presets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    access_key: Mapped[str] = mapped_column(Text, nullable=False)
    secret_key: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet-encrypted, see app/services/secrets.py
    use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DriveStorageTarget(Base):
    """One S3 target for a drive: exactly one MASTER, up to two SLAVE
    (enforced at the application level, not by a DB constraint — see
    owload-docs/decisions/0011). Either preset_id or custom_config is set,
    never both (DB CHECK constraint, see migration)."""

    __tablename__ = "drive_storage_targets"
    __table_args__ = (
        CheckConstraint(
            "(preset_id IS NOT NULL) != (custom_config IS NOT NULL)",
            name="ck_drive_storage_targets_exactly_one_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drives.id", ondelete="CASCADE"), nullable=False, index=True
    )
    preset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("s3_presets.id"), nullable=True, index=True
    )
    # Same connection fields as S3Preset (plus a Fernet-encrypted secret_key),
    # inline instead of a preset row; always hot (no tier field — a user
    # supplying their own connection isn't offered a cold-tier choice).
    custom_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=StorageTargetStatus.ACTIVE)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    preset: Mapped[S3Preset | None] = relationship()
