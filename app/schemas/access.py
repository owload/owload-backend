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
