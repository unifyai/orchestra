from pydantic import BaseModel


class EvalBatchResponse(BaseModel):
    """
    Response model for batch evaluation of prompts.
    """

    info: str
