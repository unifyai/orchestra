from pydantic import BaseModel


class CustomApiKeyModelResponse(BaseModel):
    key: str
    value: str
