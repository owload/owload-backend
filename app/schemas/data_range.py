from pydantic import BaseModel


class DataRangeOut(BaseModel):
    start: int
    end: int
