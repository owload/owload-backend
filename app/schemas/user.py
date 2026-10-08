from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


# The preset pictures of the profile; the client draws them. Keep in step with owload-front
# (src/components/profile/avatar-presets.tsx). The initials of the user's name are the default (kind "default"), not a preset.
AVATAR_PRESETS = (
    "rings", "arcs", "split", "stripes", "dots", "waves", "cipher", "peaks", "keyhole", "moon", "bars",
)

# The largest file accepted as a profile picture; the server scales it down and stores a small JPEG (the client scales it
# first, so a normal upload is a few kilobytes).
AVATAR_UPLOAD_MAX_BYTES = 10 * 1024 * 1024


class AvatarOut(BaseModel):
    """`default`: nothing chosen, the client shows the initials of the name. `preset`: one of AVATAR_PRESETS. `upload`: the
    user's image, fetched from GET /userinfo/avatar; `version` changes with every upload (for the cache)."""

    kind: Literal["default", "preset", "upload"]
    preset: str | None = None
    version: int | None = None


class UserBasicInfoOut(BaseModel):
    """Constructed explicitly from a User row in route code (not from_attributes) —
    the ORM's `id` column maps to the wire field `userId`."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(alias="userId")
    email: str
    name: str
    avatar: AvatarOut = AvatarOut(kind="default")
    # When the user first signed in to Owload (the profile is created on the first call); only the signed-in user's profile has it.
    member_since: datetime | None = Field(default=None, alias="memberSince")
    # When the user was last seen before this visit (None the first time); only the signed-in user's profile has it.
    last_seen: datetime | None = Field(default=None, alias="lastSeen")


class SetAvatarPresetIn(BaseModel):
    preset: str

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, value: str) -> str:
        if value not in AVATAR_PRESETS:
            raise ValueError("Unknown preset")
        return value


class SaveUserBasicInfoIn(BaseModel):
    """Only the name can be changed; the email always comes from the sign-in token. An empty name clears it."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]


class UserSessionOut(BaseModel):
    """A sign-in of the user: where from and when. `current` is the session of the request."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    device: str
    mobile: bool
    ip: str | None
    started_at: datetime = Field(alias="startedAt")
    last_seen_at: datetime = Field(alias="lastSeenAt")
    current: bool


class UserEventOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    kind: str
    at: datetime
    ip: str | None
    device: str | None
    details: dict
    session_id: str | None = Field(alias="sessionId")
    current_session: bool = Field(alias="currentSession")


class UserActivityPageOut(BaseModel):
    """A page of the events of the user, newest first. `next` is the cursor for the page after it (None at the end)."""

    items: list[UserEventOut]
    next: int | None
