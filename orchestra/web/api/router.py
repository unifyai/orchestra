from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import chat_completion, image_generation, monitoring, user
from orchestra.web.api.dependencies import auth_api_key

AUTH = [Depends(auth_api_key)]

api_router = APIRouter()
api_router.include_router(monitoring.router)
api_router.include_router(user.router, prefix="/user", tags=["user"])
api_router.include_router(
    chat_completion.router,
    tags=["chat_completion"],
    dependencies=AUTH,
)
api_router.include_router(
    image_generation.router,
    tags=["image_generation"],
    dependencies=AUTH,
)
