from pydantic import BaseModel


class CreditsResponse(BaseModel):
    """
    Response model for credits models.

    Attributes:
        id (str): The id of the users.
        credits (float): The credits of the users.
    """

    id: str
    credits: float


class CreditsCodeResponse(BaseModel):
    """
    Response model for credits code models.

    Attributes:
        msg (str): Message returned to the user.
    """

    msg: str
