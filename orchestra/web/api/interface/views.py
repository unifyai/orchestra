import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette import status

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.web.api.interface.schema import InterfaceConfig

router = APIRouter()


@router.post(
    "/interface",
    responses={
        201: {
            "description": "Interface created.",
            "content": {
                "application/json": {
                    "example": {"info": "Interface created successfully!"},
                },
            },
        },
        400: {
            "description": "Interface already exists.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Interface already exists, update the interface instead.",
                    },
                },
            },
        },
    },
)
def create_interface(
    request_fastapi: Request,
    request: InterfaceConfig,
    interface_dao: InterfaceDAO = Depends(),
):
    interface = interface_dao.get_interface(request_fastapi.state.user_id)
    if interface:
        raise HTTPException(
            status_code=400,
            detail="Interface already exists, update the interface instead.",
        )
    interface_dao.create_interface(
        user_id=request_fastapi.state.user_id,
        items=json.dumps([item.model_dump() for item in request.items]),
        new_counter=request.new_counter,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"info": "Interface created successfully!"},
    )


@router.put(
    "/interface",
    responses={
        200: {
            "description": "Interface updated.",
            "content": {
                "application/json": {"info": "Interface updated successfully!"},
            },
        },
        404: {
            "description": "Interface Not Found",
            "content": {
                "application/json": {
                    "example": "Interface not added yet. Create it first.",
                },
            },
        },
    },
)
def update_interface(
    request_fastapi: Request,
    request: InterfaceConfig,
    interface_dao: InterfaceDAO = Depends(),
):
    interface = interface_dao.get_interface(request_fastapi.state.user_id)
    if not interface:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )
    interface_dao.update_interface(
        user_id=request_fastapi.state.user_id,
        items=json.dumps([item.model_dump() for item in request.items]),
        new_counter=request.new_counter,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"info": "Interface updated successfully!"},
    )


@router.get(
    "/interface",
    responses={
        200: {
            "description": "Interface retrieved.",
            "content": {
                "application/json": {
                    "example": {
                        "items": [
                            {"i": "n0", "x": 0, "y": 0, "w": 3, "h": 3, "tab": None},
                            {
                                "i": "n1",
                                "x": 0,
                                "y": 1,
                                "w": 2,
                                "h": 3,
                                "tab": "Plot_1",
                            },
                        ],
                        "new_counter": 2,
                    },
                },
            },
        },
        404: {
            "description": "Interface Not Found",
            "content": {
                "application/json": {
                    "example": "Interface not added yet. Create it first.",
                },
            },
        },
    },
)
def get_interface(
    request_fastapi: Request,
    interface_dao: InterfaceDAO = Depends(),
):
    interface = interface_dao.get_interface(request_fastapi.state.user_id)
    if not interface:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )
    return interface
