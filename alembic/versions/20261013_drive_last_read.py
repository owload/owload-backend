"""When anyone last read a drive: `drives.last_read_at`.

Revision ID: 20261013_drive_last_read
Revises: 20261012_shared_only_acl
Create Date: 2026-10-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261013_drive_last_read"
down_revision: Union[str, None] = "20261012_shared_only_acl"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE drives ADD COLUMN IF NOT EXISTS last_read_at TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    op.execute("ALTER TABLE drives DROP COLUMN IF EXISTS last_read_at")
