from pydantic import BaseModel


class ProviderModelResponse(BaseModel):
    """
    Response model for provider models.

    Attributes:
        id (int): The id of the provider.
        name (str): The name of the provider.
        image_url (str): The image url of the provider.
        description (str): The description of the provider.
    """

    id: int
    name: str
    image_url: str
    description: str
