"""Three preset pictures ("stripes", "arcs", "rings") were replaced ("tiles", "corner", "orbit"): whoever had chosen one goes back to the initials.

Revision ID: 20261017_avatar_presets_replaced
Revises: 20261016_storage_limits
Create Date: 2026-10-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261017_avatar_presets_replaced"
down_revision: Union[str, None] = "20261016_storage_limits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM user_avatars WHERE preset IN ('stripes', 'arcs', 'rings', 'overlap')")


def downgrade() -> None:
    pass  # the choices that were removed cannot come back
