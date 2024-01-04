from pydantic import BaseModel


class LicenseModelResponse(BaseModel):
    """
    Response model for license models.

    Attributes:
        name (str): The name of the license.
        image_url (str): The image url of the license.
        description (str): The description of the license.
    """

    name: str
    image_url: str
    description: str
