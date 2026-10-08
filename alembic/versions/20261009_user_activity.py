"""Account activity: the user_sessions and user_events tables.

Revision ID: 20261009_user_activity
Revises: 20261008_user_avatars
Create Date: 2026-10-09
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261009_user_activity"
down_revision: Union[str, None] = "20261008_user_avatars"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            id VARCHAR(255) PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            ip VARCHAR(45),
            user_agent VARCHAR(512),
            device VARCHAR(120) NOT NULL DEFAULT 'Unknown device',
            mobile BOOLEAN NOT NULL DEFAULT false,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            prev_seen_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_sessions_user_id ON user_sessions (user_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_events (
            id BIGSERIAL PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            session_id VARCHAR(255),
            ip VARCHAR(45),
            device VARCHAR(120),
            details JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_events_user_id ON user_events (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_events_created_at ON user_events (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_events")
    op.execute("DROP TABLE IF EXISTS user_sessions")
