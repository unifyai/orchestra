from pydantic import BaseModel, Field


class DatasetInfo(BaseModel):
    name: str = Field(
        description="A unique, user-defined name assigned to the dataset.",
        json_schema_extra={"example": "eval-project"},
    )


class DatasetNewName(BaseModel):
    name: str = Field(
        description="New name of the dataset.",
        json_schema_extra={"example": "renamed-dataset"},
    )
