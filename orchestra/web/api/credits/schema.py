from pydantic import BaseModel


class CreditsRequest(BaseModel):
    """
    Request model for creating new credits model.

    Attributes:
        id (str): The id of the users.
    """

    id: str


class CreditsResponse(BaseModel):
    """
    Response model for credits models.

    Attributes:
        id (str): The id of the users.
        credits (float): The credits of the users.
    """

    id: str
    credits: float
