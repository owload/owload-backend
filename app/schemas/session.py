from pydantic import BaseModel, ConfigDict, Field


class SessionInfoOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    byte_offset: int = Field(alias="byteOffset")
    byte_length: int = Field(alias="byteLength")
    drive_id: str = Field(alias="driveId")
    created_timestamp: int = Field(alias="createdTimestamp")
    last_write_timestamp: int = Field(alias="lastWriteTimestamp")
    status: int
