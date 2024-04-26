from pydantic import BaseModel


class EvalBatchResponse(BaseModel):
    """
    Response model for batch evaluation of prompts.
    """

    info: str


class EvalBatchTaskResponse(BaseModel):
    """
    Response model for batch evaluation tasks
    """

    id: int
    user_id: str
    name: str
    status: str
