from pydantic import BaseModel, ConfigDict, Field


class UserBasicInfoOut(BaseModel):
    """Constructed explicitly from a User row in route code (not from_attributes) —
    the ORM's `id` column maps to the wire field `userId`."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(alias="userId")
    email: str
    name: str


class SaveUserBasicInfoIn(BaseModel):
    name: str
