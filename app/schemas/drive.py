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


class AccessPersonOut(BaseModel):
    """Someone on a drive, by name only (decisions/0039). `isMe` marks the person who asks."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    is_me: bool = Field(alias="isMe")
    role: str


class RecentReadOut(BaseModel):
    """The last time somebody opened or read the drive (milliseconds), with their role and which of the two it was."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(alias="userId")
    name: str
    at: int
    role: str
    kind: str
    device: str | None = None


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
    # "private", "shared" or "public" (app/services/access.py), and what the caller may do on the drive: "owner", "admin",
    # "writer" or "reader". Only an owner or an admin gets the ACL (people and roles); for others it is empty.
    visibility: str = "private"
    my_role: str = Field(default="owner", alias="myRole")
    people_count: int = Field(default=0, alias="peopleCount")
    owner_name: str | None = Field(default=None, alias="ownerName")
    owner_email: str | None = Field(default=None, alias="ownerEmail")
    # What the server knows about a drive without its key: when anyone last read it (milliseconds, null if never) and how many bytes
    # its blocks really take in the storage (live files, old versions and thumbnails together: the server cannot tell them apart;
    # freed blocks are not counted, unlike the end of the reserved area).
    last_read_at: int | None = Field(default=None, alias="lastReadAt")
    # null while it has not been counted yet (the first list after a drive is created or after the server has been updated)
    stored_bytes: int | None = Field(default=None, alias="storedBytes")
    # The most the drive may store, set by its owner; null: none of its own (the owner's limit is the one that holds it).
    storage_limit_bytes: int | None = Field(default=None, alias="storageLimitBytes")
    # Who is on the drive, for its card (decisions/0039): at most five of them, how many people that is in all, the roles of the others as counts (the
    # owner and the admins only), the link of a public drive (the owner and the admins only) and who read it last (everyone on the drive).
    access_people: list[AccessPersonOut] = Field(default_factory=list, alias="accessPeople")
    access_total: int = Field(default=1, alias="accessTotal")
    access_roles: dict[str, int] = Field(default_factory=dict, alias="accessRoles")
    public_link: str | None = Field(default=None, alias="publicLink")
    recent_reads: list[RecentReadOut] = Field(default_factory=list, alias="recentReads")


class StorageLimitIn(BaseModel):
    """The owner's limit of a drive in bytes, or null to have none of its own."""

    model_config = ConfigDict(populate_by_name=True)

    limit_bytes: int | None = Field(alias="limitBytes", ge=1, le=2**50)
