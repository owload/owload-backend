"""Requests for access to a drive (decisions/0037): `drive_access_requests`.

Revision ID: 20261018_access_requests
Revises: 20261017_avatar_presets_replaced
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261018_access_requests"
down_revision: Union[str, None] = "20261017_avatar_presets_replaced"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_access_requests (
            id UUID PRIMARY KEY,
            drive_id UUID NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL,
            message TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT uq_drive_access_requests_drive_user UNIQUE (drive_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_drive_access_requests_drive_id ON drive_access_requests (drive_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_drive_access_requests_user_id ON drive_access_requests (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS drive_access_requests")
