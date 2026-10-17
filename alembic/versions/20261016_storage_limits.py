"""Storage limits: `drives.storage_limit_bytes` (the owner's own limit of a drive) and `upload_sessions.written_bytes` (how much of a
reserved region has been written).

Revision ID: 20261016_storage_limits
Revises: 20261015_target_stored_bytes
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261016_storage_limits"
down_revision: Union[str, None] = "20261015_target_stored_bytes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE drives ADD COLUMN IF NOT EXISTS storage_limit_bytes BIGINT")
    op.execute("ALTER TABLE upload_sessions ADD COLUMN IF NOT EXISTS written_bytes BIGINT NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE upload_sessions DROP COLUMN IF EXISTS written_bytes")
    op.execute("ALTER TABLE drives DROP COLUMN IF EXISTS storage_limit_bytes")
