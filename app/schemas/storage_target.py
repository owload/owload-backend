import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.drive import CustomStorageConfigIn


class AddStorageTargetIn(BaseModel):
    """Adding a slave target — unlike the drive's initial (master) target,
    a preset of either tier is allowed here (only master is hot-only)."""

    model_config = ConfigDict(populate_by_name=True)

    preset_id: uuid.UUID | None = Field(default=None, alias="presetId")
    custom_config: CustomStorageConfigIn | None = Field(default=None, alias="customConfig")

    @model_validator(mode="after")
    def _check_exactly_one_source(self) -> "AddStorageTargetIn":
        if (self.preset_id is None) == (self.custom_config is None):
            raise ValueError("provide exactly one of presetId or customConfig")
        return self


class StorageTargetOut(BaseModel):
    """Shape fixed by the frontend's DriveStorageTarget interface
    (owload-front/src/engine/backend/drive-backend.ts) — tier is
    lowercase ('hot'/'cold') to match its S3PresetTier union."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    role: str
    status: str
    tier: str
    preset_id: str | None = Field(default=None, alias="presetId")
    preset_label: str | None = Field(default=None, alias="presetLabel")
    is_custom: bool = Field(alias="isCustom")
    custom_endpoint_url: str | None = Field(default=None, alias="customEndpointUrl")
    custom_bucket: str | None = Field(default=None, alias="customBucket")
    # Only populated for status=PROVISIONING (owload-docs/epics/0013) — a
    # live count of objects copied so far vs. master's current total,
    # not an exact byte progress.
    backfill_copied: int | None = Field(default=None, alias="backfillCopied")
    backfill_total: int | None = Field(default=None, alias="backfillTotal")


class ConnectionTestOut(BaseModel):
    ok: bool
    error: str | None = None
