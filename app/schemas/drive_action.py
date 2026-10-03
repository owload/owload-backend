from pydantic import BaseModel, ConfigDict, Field


class DriveActionLogOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    drive_id: str = Field(alias="driveId")
    user_id: str = Field(alias="userId")
    action: str
    attributes: dict
    timestamp: int
    seq: int = Field(
        description="Keyset-pagination cursor — pass the last item's seq as `before` for the next page."
    )
