from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/docs", include_in_schema=False)
def redirect_docs():
    return RedirectResponse(url="https://docs.unify.ai/api-reference")
