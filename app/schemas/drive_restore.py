import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.drive import CustomStorageConfigIn

# Matches MAX_RESTORE_DRIVES in app/services/restore.py.
_MAX_IDS = 20


class RestoreScanIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    custom_config: CustomStorageConfigIn = Field(alias="customConfig")


class RestoreIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    custom_config: CustomStorageConfigIn = Field(alias="customConfig")
    drive_ids: list[uuid.UUID] = Field(alias="driveIds", min_length=1, max_length=_MAX_IDS)


class FoundDriveOut(BaseModel):
    """A drive found in the scanned bucket. `status` is RESTORABLE,
    ALREADY_EXISTS or DAMAGED; `reason` says why for DAMAGED."""

    model_config = ConfigDict(populate_by_name=True)

    drive_id: str = Field(alias="driveId")
    title: str | None = None
    created_timestamp: int | None = Field(default=None, alias="createdTimestamp")
    ops_bytes: int | None = Field(default=None, alias="opsBytes")
    status: str
    reason: str | None = None


class RestoreScanOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    drives: list[FoundDriveOut]
    truncated: bool


class RestoreResultOut(BaseModel):
    """`status` is RESTORED, ALREADY_EXISTS, DAMAGED, NOT_FOUND or FAILED."""

    model_config = ConfigDict(populate_by_name=True)

    drive_id: str = Field(alias="driveId")
    status: str
    reason: str | None = None


class RestoreOut(BaseModel):
    results: list[RestoreResultOut]
