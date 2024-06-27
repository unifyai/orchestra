"""
Includes endpoints for training and deployment of a router.
"""

from typing import Dict, List

from fastapi import APIRouter, Request

router = APIRouter()


# endpoints

# TODO: Allow not sending an email for the website + composed flows

# TODO: List trained routers


@router.post("router/train")
def train_router(
    request_fastapi: Request,
    name: str,
    dataset: str,
    endpoints: List[str],
) -> Dict[str, str]:
    # Check if the router already exists
    # Check if the dataset exists
    # Check that the endpoints are valid
    # Send train job to the training server
    return {"info": "Router training started! You will receive an email soon!"}


@router.delete("/router/train")
def delete_router_train(request_fastapi: Request, name: str) -> Dict[str, str]:
    """
    Deactivates and deletes a trained router.
    """
    # Check if the router files exist
    # Delete the trained router files
    return {"info": "Trained router deleted!"}


# TODO: List deployed routers


@router.post("/router/deploy")
def deploy_router(request_fastapi: Request, name: str) -> Dict[str, str]:
    """
    Deploys a router.
    """
    # Check if the files exist
    # Check if the router is already deployed
    # Send the request with the job to the router deployment service
    return {"info": "Router deployment started! You will receive an email soon!"}


@router.delete("/router")
def delete_router(request_fastapi: Request, name: str) -> Dict[str, str]:
    """
    Deactivates and deletes a deployed router.
    """
    # Check if the router exists
    # Delete the entry from the database
    # Send the request with the job to the router deployment service
    return {"info": "Router deletion started! You will receive an email soon!"}
