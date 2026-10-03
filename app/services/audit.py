import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveActionLog


def log_action(db: AsyncSession, drive_id: uuid.UUID, user_id: str, action: str, **attributes) -> None:
    """Stages an audit row in the caller's existing transaction — it never
    commits on its own. Call this right before the route's own db.commit()
    so the log entry only persists if the action it describes actually
    succeeded (same all-or-nothing guarantee as the rest of the transaction)."""
    db.add(DriveActionLog(drive_id=drive_id, user_id=user_id, action=action, attributes=attributes))
