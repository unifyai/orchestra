from pydantic import BaseModel, ConfigDict, validator


class CustomEndpointModelResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: str
    model_arg: str
    url: str
    key: str

    @validator("model_arg", pre=True, always=True)
    def set_model_arg(cls, v, values):
        return v or values.get("name")
