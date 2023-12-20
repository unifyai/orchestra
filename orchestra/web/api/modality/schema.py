from pydantic import BaseModel


class ModalityModelRequest(BaseModel):
    """
    Request model for creating new modality model.

    Attributes:
        name (str): The name of the modality.
    """

    name: str


class ModalityModelResponse(BaseModel):
    """
    Response model for modality models.

    Attributes:
        name (str): The name of the modality.
    """

    name: str
