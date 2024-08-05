from pydantic import BaseModel, validator


class CustomApiKeyModelResponse(BaseModel):
    key: str
    value: str


class CustomEndpointModelResponse(BaseModel):
    name: str
    mdl_name: str
    url: str
    key: str

    @validator("mdl_name", pre=True, always=True)
    def set_mdl_name(cls, v, values):
        return v or values.get("name")
