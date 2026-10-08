"""An index for finding the latest event of a kind of a user (the squashing of bursts of drive access).

Revision ID: 20261010_user_events_by_kind
Revises: 20261009_user_activity
Create Date: 2026-10-10
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261010_user_events_by_kind"
down_revision: Union[str, None] = "20261009_user_activity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_events_user_kind ON user_events (user_id, kind, id DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_events_user_kind")
