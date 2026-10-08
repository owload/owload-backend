"""People belong to shared drives only: clear the ACL and the invitations of drives that are private or public.

Revision ID: 20261012_shared_only_acl
Revises: 20261011_drive_access
Create Date: 2026-10-12
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20261012_shared_only_acl"
down_revision: Union[str, None] = "20261011_drive_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM drive_invitations WHERE drive_id IN (SELECT id FROM drives WHERE visibility <> 'shared')")
    op.execute("UPDATE drives SET acl = '{}'::jsonb WHERE visibility <> 'shared' AND acl <> '{}'::jsonb")


def downgrade() -> None:
    pass
