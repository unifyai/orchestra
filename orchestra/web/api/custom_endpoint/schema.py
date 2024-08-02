from pydantic import BaseModel


class CustomApiKeyModelResponse(BaseModel):
    key: str
    value: str


class CustomEndpointModelResponse(BaseModel):
    name: str
    mdl_name: str
    url: str
    key: str
