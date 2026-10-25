"""Anonymous reads of public drives (decisions/0040): `public_drive_reads`.

Revision ID: 20261019_public_drive_reads
Revises: 20261018_access_requests
Create Date: 2026-10-25
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261019_public_drive_reads"
down_revision: Union[str, None] = "20261018_access_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public_drive_reads (
            drive_id UUID NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
            device VARCHAR(120) NOT NULL,
            kind VARCHAR(16) NOT NULL,
            last_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (drive_id, device)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public_drive_reads")
