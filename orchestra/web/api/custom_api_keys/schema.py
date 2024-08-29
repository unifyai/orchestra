from pydantic import BaseModel


class CustomApiKeyModelResponse(BaseModel):
    name: str
    value: str
