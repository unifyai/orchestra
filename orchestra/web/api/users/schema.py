from pydantic import BaseModel


class UsersModelResponse(BaseModel):
    """
    Response model for users models.

    Attributes:
        id (str): The id of the users.
        credits (float): The credits of the users.
    """

    id: str
    credits: float
