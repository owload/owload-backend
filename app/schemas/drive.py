import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CustomStorageConfigIn(BaseModel):
    """Caller-supplied S3 connection params for a drive's initial target,
    used instead of a curated preset. Always treated as hot tier (see
    owload-docs/decisions/0011) — there's no separate "custom cold" concept."""

    model_config = ConfigDict(populate_by_name=True)

    endpoint_url: str | None = Field(default=None, alias="endpointUrl")
    region: str
    bucket: str
    access_key: str = Field(alias="accessKey")
    secret_key: str = Field(alias="secretKey")
    use_ssl: bool = Field(default=True, alias="useSsl")


class CreateDriveIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str
    key_nonce: str = Field(alias="keyNonce")
    counter_nonce: str = Field(alias="counterNonce")
    preset_id: uuid.UUID | None = Field(default=None, alias="presetId")
    custom_config: CustomStorageConfigIn | None = Field(default=None, alias="customConfig")

    @model_validator(mode="after")
    def _check_exclusive_storage_selection(self) -> "CreateDriveIn":
        if self.preset_id is not None and self.custom_config is not None:
            raise ValueError("provide either presetId or customConfig, not both")
        return self


class CustomTargetDeletionDecision(BaseModel):
    """One drive owner's explicit choice for a single custom-config target
    when deleting the whole drive (owload-docs/decisions/0013) — never
    inferred, one required per custom target the drive actually has."""

    model_config = ConfigDict(populate_by_name=True)

    target_id: uuid.UUID = Field(alias="targetId")
    delete_data: bool = Field(alias="deleteData")


class DeleteDriveIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    custom_target_decisions: list[CustomTargetDeletionDecision] = Field(
        default_factory=list, alias="customTargetDecisions"
    )


class DriveInfoOut(BaseModel):
    """Field names/casing match the frontend's DriveInfo interface exactly,
    including the all-caps "ACL" key."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    owner_user_id: str = Field(alias="ownerUserId")
    title: str
    acl: dict = Field(alias="ACL")
    created_timestamp: int = Field(alias="createdTimestamp")
    key_nonce: str = Field(alias="keyNonce")
    counter_nonce: str = Field(alias="counterNonce")
