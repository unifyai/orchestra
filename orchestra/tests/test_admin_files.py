import base64
from typing import Dict

import pytest
from fastapi import status

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_admin_file_endpoints_end_to_end(client):
    # Create a test user and a project
    user = await create_test_user(client, email="filetests@example.com")
    project_name = "files-project"
    create_project_resp = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=user["headers"],
    )
    assert (
        create_project_resp.status_code == status.HTTP_200_OK
    ), create_project_resp.json()

    user_id = user["id"]

    # Prepare multiple files (text and binary) as base64 strings
    original_files: Dict[str, bytes] = {
        "text/hello.txt": b"Hello, World!",
        "bin/data.bin": b"\x00\x01\x02\x03\x04\x05",
        "img/sample.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0D",
    }
    files_payload = {
        path: base64.b64encode(content).decode("ascii")
        for path, content in original_files.items()
    }

    # 1) Write files
    write_resp = await client.post(
        "/v0/admin/file",
        json={
            "user_id": user_id,
            "project": project_name,
            "files": files_payload,
            "staging": True,
        },
        headers=ADMIN_HEADERS,
    )
    assert write_resp.status_code == status.HTTP_200_OK, write_resp.json()

    # 2) List files and verify all are present and contents match (base64)
    list_resp = await client.get(
        f"/v0/admin/file?user_id={user_id}&project={project_name}&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert list_resp.status_code == status.HTTP_200_OK, list_resp.json()
    listed = list_resp.json()
    assert set(listed.keys()) == set(original_files.keys())
    for path, original_bytes in original_files.items():
        assert listed[path] == base64.b64encode(original_bytes).decode("ascii")

    # 3) Read each file via contents endpoint and verify round-trip
    for path, original_bytes in original_files.items():
        read_resp = await client.get(
            f"/v0/admin/file/contents?user_id={user_id}&project={project_name}&path={path}&staging=true",
            headers=ADMIN_HEADERS,
        )
        assert read_resp.status_code == status.HTTP_200_OK, read_resp.json()
        data = read_resp.json()
        assert data["path"].endswith(f"/{path}")
        got_bytes = base64.b64decode(data["contents"])
        assert got_bytes == original_bytes

    # 4) Delete a single file, verify it is gone
    del_single_path = "text/hello.txt"
    del_single_resp = await client.delete(
        f"/v0/admin/file?user_id={user_id}&project={project_name}&path={del_single_path}&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert del_single_resp.status_code == status.HTTP_200_OK, del_single_resp.json()

    # Confirm not listed anymore
    list_after_single = await client.get(
        f"/v0/admin/file?user_id={user_id}&project={project_name}&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert list_after_single.status_code == status.HTTP_200_OK
    listed_after_single = list_after_single.json()
    assert del_single_path not in listed_after_single

    # Reading should 404 now
    read_deleted = await client.get(
        f"/v0/admin/file/contents?user_id={user_id}&project={project_name}&path={del_single_path}&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert read_deleted.status_code == status.HTTP_404_NOT_FOUND

    # 4b) Delete a folder (recursive) and verify files under it are gone
    del_folder = "bin"
    del_folder_resp = await client.delete(
        f"/v0/admin/file?user_id={user_id}&project={project_name}&path={del_folder}&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert del_folder_resp.status_code == status.HTTP_200_OK, del_folder_resp.json()

    list_after_folder = await client.get(
        f"/v0/admin/file?user_id={user_id}&project={project_name}&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert list_after_folder.status_code == status.HTTP_200_OK
    listed_after_folder = list_after_folder.json()
    assert "bin/data.bin" not in listed_after_folder
    # Ensure the remaining image still exists
    assert "img/sample.png" in listed_after_folder

    # Cleanup remaining test data (delete img/ folder)
    del_img_resp = await client.delete(
        f"/v0/admin/file?user_id={user_id}&project={project_name}&path=img&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert del_img_resp.status_code == status.HTTP_200_OK, del_img_resp.json()


@pytest.mark.anyio
async def test_admin_file_signed_url_endpoints(client):
    # Create a test user and a project
    user = await create_test_user(client, email="filetests-signed@example.com")
    project_name = "files-project-signed"
    create_project_resp = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=user["headers"],
    )
    assert (
        create_project_resp.status_code == status.HTTP_200_OK
    ), create_project_resp.json()

    user_id = user["id"]

    # 1) Create upload URL (resumable)
    req = {
        "user_id": user_id,
        "project": project_name,
        "path": "signed/file.bin",
        "content_type": "application/octet-stream",
        "staging": True,
    }
    up_resp = await client.post(
        "/v0/admin/file/upload_url",
        json=req,
        headers=ADMIN_HEADERS,
    )
    assert up_resp.status_code == status.HTTP_200_OK, up_resp.json()
    up_data = up_resp.json()
    assert "upload_url" in up_data and "path" in up_data
    assert up_data["path"].endswith("/signed/file.bin")

    # 2) Write a real small object so download URL can be generated
    content_bytes = b"small-object"
    put_payload = {
        "user_id": user_id,
        "project": project_name,
        "files": {"signed/file.bin": base64.b64encode(content_bytes).decode("ascii")},
        "staging": True,
    }
    put_resp = await client.post(
        "/v0/admin/file",
        json=put_payload,
        headers=ADMIN_HEADERS,
    )
    assert put_resp.status_code == status.HTTP_200_OK, put_resp.json()

    # 3) Create download URL
    down_resp = await client.get(
        f"/v0/admin/file/download_url?user_id={user_id}&project={project_name}&path=signed/file.bin&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert down_resp.status_code == status.HTTP_200_OK, down_resp.json()
    down_data = down_resp.json()
    assert "download_url" in down_data and "path" in down_data
    assert down_data["path"].endswith("/signed/file.bin")

    # 4) Invalid path should 400
    bad_req = {
        "user_id": user_id,
        "project": project_name,
        "path": "../escape.bin",
        "staging": True,
    }
    bad_resp = await client.post(
        "/v0/admin/file/upload_url",
        json=bad_req,
        headers=ADMIN_HEADERS,
    )
    assert bad_resp.status_code == status.HTTP_400_BAD_REQUEST

    bad_get = await client.get(
        f"/v0/admin/file/download_url?user_id={user_id}&project={project_name}&path=../escape.bin&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert bad_get.status_code == status.HTTP_400_BAD_REQUEST

    # 5) Unknown project should 404
    no_proj_req = {
        "user_id": user_id,
        "project": "does-not-exist",
        "path": "file.bin",
    }
    no_proj_resp = await client.post(
        "/v0/admin/file/upload_url",
        json=no_proj_req,
        headers=ADMIN_HEADERS,
    )
    assert no_proj_resp.status_code == status.HTTP_404_NOT_FOUND

    # Cleanup the uploaded object
    del_resp = await client.delete(
        f"/v0/admin/file?user_id={user_id}&project={project_name}&path=signed&staging=true",
        headers=ADMIN_HEADERS,
    )
    assert del_resp.status_code == status.HTTP_200_OK, del_resp.json()
