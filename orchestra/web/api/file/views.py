from fastapi import APIRouter, Depends, HTTPException, Query, Request
from google.cloud.storage import Client

from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.web.api.file.schema import FileUploadRequest

router = APIRouter()


@router.post(
    "/file",
    responses={
        200: {
            "description": "File uploaded successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "File uploaded successfully",
                        "path": "123/my-project/my/file/path.txt",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def upload_file(
    request_fastapi: Request,
    request: FileUploadRequest,
    project_dao: ProjectDAO = Depends(),
):
    """
    Upload a file to the Google Cloud Storage bucket.
    The file will be stored at <user-id>/<project>/<path>
    """
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=request.project,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket("interface-file-system")

        # Construct the full path in the bucket
        full_path = f"{request_fastapi.state.user_id}/{project.name}/{request.path}"

        # Create a new blob and upload the file contents
        blob = bucket.blob(full_path)
        blob.upload_from_string(request.contents)

        return {
            "message": "File uploaded successfully",
            "path": full_path,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload file: {str(e)}",
        )


@router.get(
    "/file",
    responses={
        200: {
            "description": "List of files retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "files": [
                            "123/my-project/file1.txt",
                            "123/my-project/folder/file2.txt",
                        ],
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def list_files(
    request_fastapi: Request,
    project: str = Query(..., description="Name of the project"),
    project_dao: ProjectDAO = Depends(),
):
    """
    List all files in a user's project folder in the bucket.
    Returns a flat list of file paths.
    """
    project_obj = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket("interface-file-system")

        # Construct the prefix to list files under
        prefix = f"{request_fastapi.state.user_id}/{project_obj.name}/"

        # List all blobs under the prefix
        blobs = bucket.list_blobs(prefix=prefix)

        # Extract the full paths
        files = [blob.name for blob in blobs]

        return {
            "files": files,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list files: {str(e)}",
        )


@router.get(
    "/file/contents",
    responses={
        200: {
            "description": "File contents retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "contents": "Hello, world!",
                        "path": "my-app/folder/file.txt",
                    },
                },
            },
        },
        404_1: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "File Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "File not found at path: <path>",
                    },
                },
            },
        },
    },
)
def get_file_contents(
    request_fastapi: Request,
    project: str = Query(..., description="Name of the project"),
    path: str = Query(..., description="Path to the file in the bucket"),
    project_dao: ProjectDAO = Depends(),
):
    """
    Get the contents of a specific file in the bucket.
    """
    project_obj = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket("interface-file-system")

        # Construct the full path in the bucket
        full_path = f"{request_fastapi.state.user_id}/{project_obj.name}/{path}"

        # Get the blob
        blob = bucket.blob(full_path)

        # Check if the file exists
        if not blob.exists():
            raise HTTPException(
                status_code=404,
                detail=f"File not found at path: {full_path}",
            )

        # Download the contents
        contents = blob.download_as_text()

        return {
            "contents": contents,
            "path": full_path,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get file contents: {str(e)}",
        )


@router.delete(
    "/file",
    responses={
        200: {
            "description": "File or folder deleted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "File or folder deleted successfully",
                        "path": "my-app/folder/file.txt",
                    },
                },
            },
        },
        404: {
            "description": "Project or File Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found or file not found at path: <path>",
                    },
                },
            },
        },
    },
)
def delete_file_or_folder(
    request_fastapi: Request,
    project: str = Query(..., description="Name of the project"),
    path: str = Query(..., description="Path to the file or folder in the bucket"),
    project_dao: ProjectDAO = Depends(),
):
    """
    Delete a file or folder from the user's project directory.
    If the path points to a folder, all contents will be deleted recursively.
    """
    project_obj = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket("interface-file-system")

        # Construct the full path in the bucket
        full_path = f"{request_fastapi.state.user_id}/{project_obj.name}/{path}"

        # Check if the path exists
        blobs = list(bucket.list_blobs(prefix=full_path))
        if not blobs:
            raise HTTPException(
                status_code=404,
                detail=f"File or folder not found at path: {full_path}",
            )

        # Delete all blobs under the path (handles both files and folders)
        for blob in blobs:
            blob.delete()

        return {
            "message": "File or folder deleted successfully",
            "path": full_path,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete file or folder: {str(e)}",
        )
