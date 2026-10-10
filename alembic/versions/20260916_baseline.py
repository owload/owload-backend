"""The tables that existed before the first migration of this repository, for a database that is empty.

Revision ID: 20260916_baseline
Revises:
Create Date: 2026-09-16

The repository held no migration for these tables: they were made by hand in the first databases. A new installation (an empty
database, as with the Docker image) needs them, so this creates them, unless the database already has them (then it does nothing).
Later migrations add what came after: avatars, activity, access to drives.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATEMENTS = (
    """
    CREATE TABLE users (
        id VARCHAR(255) PRIMARY KEY,
        email VARCHAR(320) NOT NULL,
        name VARCHAR(255) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_users_email ON users (email)",
    """
    CREATE TABLE drives (
        id UUID PRIMARY KEY,
        owner_user_id VARCHAR(255) NOT NULL,
        title TEXT NOT NULL,
        acl JSONB NOT NULL,
        key_nonce TEXT NOT NULL,
        counter_nonce TEXT NOT NULL,
        ops_data BYTEA NOT NULL,
        data_length BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_drives_owner_user_id ON drives (owner_user_id)",
    """
    CREATE TABLE s3_presets (
        id UUID PRIMARY KEY,
        label VARCHAR(255) NOT NULL,
        tier VARCHAR(16) NOT NULL,
        endpoint_url TEXT,
        region VARCHAR(64) NOT NULL,
        bucket VARCHAR(255) NOT NULL,
        access_key TEXT NOT NULL,
        secret_key TEXT NOT NULL,
        use_ssl BOOLEAN NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE drive_storage_targets (
        id UUID PRIMARY KEY,
        drive_id UUID NOT NULL REFERENCES drives (id) ON DELETE CASCADE,
        preset_id UUID REFERENCES s3_presets (id),
        custom_config JSONB,
        role VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_drive_storage_targets_exactly_one_source CHECK ((preset_id IS NOT NULL) <> (custom_config IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_drive_storage_targets_drive_id ON drive_storage_targets (drive_id)",
    "CREATE INDEX ix_drive_storage_targets_preset_id ON drive_storage_targets (preset_id)",
    """
    CREATE TABLE upload_sessions (
        id UUID PRIMARY KEY,
        drive_id UUID NOT NULL REFERENCES drives (id) ON DELETE CASCADE,
        byte_offset BIGINT NOT NULL,
        byte_length BIGINT NOT NULL,
        status SMALLINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_write_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX ix_upload_sessions_drive_id ON upload_sessions (drive_id)",
    "CREATE SEQUENCE drive_action_log_seq_seq",
    """
    CREATE TABLE drive_action_log (
        id UUID PRIMARY KEY,
        drive_id UUID NOT NULL REFERENCES drives (id) ON DELETE CASCADE,
        user_id VARCHAR(255) NOT NULL,
        action VARCHAR(32) NOT NULL,
        attributes JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        seq BIGINT NOT NULL DEFAULT nextval('drive_action_log_seq_seq')
    )
    """,
    "ALTER SEQUENCE drive_action_log_seq_seq OWNED BY drive_action_log.seq",
    "CREATE INDEX ix_drive_action_log_action ON drive_action_log (action)",
    "CREATE INDEX ix_drive_action_log_created_at ON drive_action_log (created_at)",
    "CREATE INDEX ix_drive_action_log_drive_id ON drive_action_log (drive_id)",
    "CREATE INDEX ix_drive_action_log_seq ON drive_action_log (seq)",
    "CREATE INDEX ix_drive_action_log_user_id ON drive_action_log (user_id)",
)


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("drives"):
        return  # a database that already has the tables (made by hand, or by an older version)
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    pass
