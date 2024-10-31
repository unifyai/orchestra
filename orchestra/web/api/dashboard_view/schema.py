from pydantic import BaseModel, Field


class DashboardViewInfo(BaseModel):
    project_id: int = Field()
    name: str = Field(
        description="A unique, user-defined name identify a new dashboard view.",
        json_schema_extra={"example": "new-dashboard-view"},
    )
    view: str = Field()


class DashboardViewNewName(BaseModel):
    project_id: int = Field()
    name: str = Field()
    new_name: str = Field(
        description="New name of the dashboard view.",
        json_schema_extra={"example": "renamed-dashboard-view"},
    )


class DashboardViewDelete(BaseModel):
    project_id: int = Field()
    name: str = Field()
