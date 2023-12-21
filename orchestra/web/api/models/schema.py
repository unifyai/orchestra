from typing import List

from pydantic import BaseModel


class ModelInfo(BaseModel):
    """
    Model information.

    The id has format {uploaded_by}/{model_name}. E.g. fair/llama-2-70b-chat
    If it doesn't include a name before the model_name (llama-2-70b-chat) it
    means that the model has been uploaded by Unify. The models uploaded
    by us can include off-the-shelf pay-per-compute endpoints (popular models
    such as SD, llama, mistral...)
    """

    id: str
    modality: str
    task: str
    providers: List[str]


class ModelInfoList(BaseModel):
    """List of models."""

    models: List[ModelInfo]
