from pydantic import BaseModel, Field


class FileUploadRequest(BaseModel):
    """Schema for file upload request."""

    project: str = Field(..., description="Name of the project")
    path: str = Field(
        ...,
        description="Path where the file should be stored in the bucket",
    )
    contents: str = Field(..., description="String contents of the file to be uploaded")
