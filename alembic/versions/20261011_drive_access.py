"""Access to drives: visibility, the public link, roles in the ACL, invitations by email.

Revision ID: 20261011_drive_access
Revises: 20261010_user_events_by_kind
Create Date: 2026-10-11
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261011_drive_access"
down_revision: Union[str, None] = "20261010_user_events_by_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE drives ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'private'")
    op.execute("ALTER TABLE drives ADD COLUMN IF NOT EXISTS public_token VARCHAR(64)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_drives_public_token ON drives (public_token)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_invitations (
            id UUID PRIMARY KEY,
            drive_id UUID NOT NULL REFERENCES drives (id) ON DELETE CASCADE,
            email VARCHAR(320) NOT NULL,
            role VARCHAR(10) NOT NULL,
            invited_by VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_drive_invitations_drive_email UNIQUE (drive_id, email)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_drive_invitations_drive_id ON drive_invitations (drive_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_drive_invitations_email ON drive_invitations (email)")

    # Drives that already had people in the ACL are shared; the ACL held lists of privileges ("READ", "WRITE"), now roles.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, acl FROM drives WHERE acl <> '{}'::jsonb")).fetchall()
    for drive_id, acl in rows:
        roles = {
            user_id: value if isinstance(value, str) else ("writer" if "WRITE" in (value or []) else "reader")
            for user_id, value in (acl or {}).items()
        }
        bind.execute(
            sa.text("UPDATE drives SET acl = CAST(:acl AS jsonb), visibility = 'shared' WHERE id = :id"),
            {"acl": json.dumps(roles), "id": drive_id},
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS drive_invitations")
    op.execute("DROP INDEX IF EXISTS ix_drives_public_token")
    op.execute("ALTER TABLE drives DROP COLUMN IF EXISTS public_token")
    op.execute("ALTER TABLE drives DROP COLUMN IF EXISTS visibility")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email_verified")
