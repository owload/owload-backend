from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VisibilityIn(BaseModel):
    visibility: Literal["private", "shared", "public"]


class AddPersonIn(BaseModel):
    email: str = Field(max_length=320)
    role: Literal["admin", "writer", "reader"]


class ChangeRoleIn(BaseModel):
    role: Literal["admin", "writer", "reader"]


class PersonOut(BaseModel):
    """Someone the drive is shared with. `invited` is a person who has no verified account with that email yet: the drive
    reaches them when they sign in with it. `id` is the user id of a member, or the id of the invitation."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    status: Literal["member", "invited"]
    email: str
    name: str | None
    role: Literal["admin", "writer", "reader"]
    since: datetime | None = None


class AccessOut(BaseModel):
    """Who can open a drive. `people` is only for an owner or an admin; `publicLink` only for them and only while the drive is
    public. `mailEnabled` says whether the backend can send the invitations itself."""

    model_config = ConfigDict(populate_by_name=True)

    visibility: Literal["private", "shared", "public"]
    my_role: Literal["owner", "admin", "writer", "reader"] = Field(alias="myRole")
    public_link: str | None = Field(default=None, alias="publicLink")
    people: list[PersonOut] = Field(default_factory=list)
    mail_enabled: bool = Field(default=False, alias="mailEnabled")


class AddPersonOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    person: PersonOut
    email_sent: bool = Field(alias="emailSent")
    app_url: str = Field(alias="appUrl")


class AccessRequestIn(BaseModel):
    """What a person tells the admins of a drive they ask to be let into (decisions/0037). Plain text, at most 500 characters."""

    message: str | None = Field(default=None, max_length=500)


class MyAccessRequestOut(BaseModel):
    """What a signed-in person is told about a drive they may not be on: that it exists, its title, and whether they have asked."""

    model_config = ConfigDict(populate_by_name=True)

    drive_id: str = Field(alias="driveId")
    has_access: bool = Field(alias="hasAccess")
    title: str
    requested: bool = False
    requested_at: datetime | None = Field(default=None, alias="requestedAt")
    message: str | None = None
    mail_enabled: bool = Field(default=False, alias="mailEnabled")


class AccessRequestOut(BaseModel):
    """A pending request, for the owner and the admins of the drive."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(alias="userId")
    name: str | None
    email: str
    message: str | None
    requested_at: datetime = Field(alias="requestedAt")


class ApproveRequestIn(BaseModel):
    role: Literal["admin", "writer", "reader"]
