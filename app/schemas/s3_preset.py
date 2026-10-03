from pydantic import BaseModel


class S3PresetOut(BaseModel):
    id: str
    label: str
    tier: str
