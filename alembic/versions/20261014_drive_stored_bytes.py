"""What a drive really stores: `drives.stored_bytes` and `drives.stored_bytes_at` (a count of its blocks in the master storage).

Revision ID: 20261014_drive_stored_bytes
Revises: 20261013_drive_last_read
Create Date: 2026-10-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261014_drive_stored_bytes"
down_revision: Union[str, None] = "20261013_drive_last_read"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE drives ADD COLUMN IF NOT EXISTS stored_bytes BIGINT")
    op.execute("ALTER TABLE drives ADD COLUMN IF NOT EXISTS stored_bytes_at TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    op.execute("ALTER TABLE drives DROP COLUMN IF EXISTS stored_bytes_at")
    op.execute("ALTER TABLE drives DROP COLUMN IF EXISTS stored_bytes")
