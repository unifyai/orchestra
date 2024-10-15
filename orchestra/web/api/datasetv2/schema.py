from typing import Any, List

from pydantic import BaseModel, Field


class DatasetInfo(BaseModel):
    name: str = Field(
        description="A unique, user-defined name identify a new dataset.",
        json_schema_extra={"example": "new-dataset"},
    )


class DatasetNewName(BaseModel):
    name: str = Field(
        description="New name of the dataset.",
        json_schema_extra={"example": "renamed-dataset"},
    )


class EntriesConfig(BaseModel):
    entries: List[Any] = Field(
        ...,
        description="List of entries to add",
        json_schema_extra={"example": ["id_1", "id_2", "id_3"]},
    )
