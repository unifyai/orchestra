"""
Includes endpoints for creating router configurations.
"""
from typing import List
from fastapi import APIRouter, Query

router = APIRouter()


# endpoints


@router.post(
    "/router/config"
)
def create_router_config(
    config_name: str = Query(
        description="The name of the router configuration to create.",
        example="cost_and_speed_optimized",
    ),
    router_endpoint: str = Query(
        description="The raw string which fully defines the router endpoint, "
                    "with all constraints applied, including the router name and any "
                    "extra arguments.",
        example="router1|models:llama-3.1-8b-chat,mixtral-8x22b-instruct-v0.1|"
                "providers:fireworks-ai,together-ai|"
                "q:1|c:4.65e-03|t:2.08e-05|i:2.07e-03@routers",
    ),
):
    """
    Creates a router configuration, which can be queried later using only the
    configuration name, such as `my_router_config@routers`.
    """
    raise NotImplemented  # ToDo: implement


@router.get(
    "/router/config"
)
def get_router_config(
    config_name: str = Query(
        description="The name of the router configuration to retrieve the "
                    "full endpoint string for.",
        example="cost_and_speed_optimized",
    ),
) -> str:
    """
    Returns the full router endpoint string for a given router configuration name.
    """
    raise NotImplemented  # ToDo: implement


@router.delete(
    "/router/config"
)
def delete_router_config(
    config_name: str = Query(
        description="The name of the router configuration to delete.",
        example="cost_and_speed_optimized",
    ),
):
    """
    Deletes the specified router configuration.
    """
    raise NotImplemented  # ToDo: implement


@router.post(
    "/router/config/rename"
)
def rename_router_config(
    name: str = Query(
        description="The original name of the router configuration.",
        example="original_name",
    ),
    new_name: str = Query(
        description="The new name for the router configuration.",
        example="new_name",
    ),
):
    """
    Renames the specified router configuration.
    """
    raise NotImplemented  # ToDo: implement


@router.get(
    "/router/config/list"
)
def list_router_configs() -> List[str]:
    """
    Lists all saved router configurations by name.
    """
    raise NotImplemented  # ToDo: implement
