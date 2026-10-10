"""The profile pictures of users: the user_avatars table.

Revision ID: 20261008_user_avatars
Revises: 20260916_baseline
Create Date: 2026-10-08

The statements are idempotent (IF NOT EXISTS): the repository holds no earlier migration, and the databases that exist
already have the other tables.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261008_user_avatars"
down_revision: Union[str, None] = "20260916_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_avatars (
            user_id VARCHAR(255) PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
            preset VARCHAR(32),
            image BYTEA,
            content_type VARCHAR(32),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_user_avatars_preset_xor_image CHECK ((preset IS NULL) <> (image IS NULL))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_avatars")
