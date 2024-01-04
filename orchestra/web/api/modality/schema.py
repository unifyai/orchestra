from pydantic import BaseModel


class ModalityModelResponse(BaseModel):
    """
    Response model for modality models.

    Attributes:
        name (str): The name of the modality.
    """

    name: str
