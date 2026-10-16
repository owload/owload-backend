"""What a drive takes in each of its storages: `drive_storage_targets.stored_bytes`.

Revision ID: 20261015_target_stored_bytes
Revises: 20261014_drive_stored_bytes
Create Date: 2026-10-15
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261015_target_stored_bytes"
down_revision: Union[str, None] = "20261014_drive_stored_bytes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE drive_storage_targets ADD COLUMN IF NOT EXISTS stored_bytes BIGINT")


def downgrade() -> None:
    op.execute("ALTER TABLE drive_storage_targets DROP COLUMN IF EXISTS stored_bytes")
