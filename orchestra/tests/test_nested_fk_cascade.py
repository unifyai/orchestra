"""
Tests for nested foreign key CASCADE operations.

This module tests CASCADE DELETE, CASCADE UPDATE, and SET NULL actions
for nested foreign keys with various path patterns:
- Array wildcards: images[*].image_id
- Nested objects: metadata.user.user_id
- Mixed nesting: teams[*].members[*].user_id
"""

import pytest
from httpx import AsyncClient

from orchestra.tests.test_log import HEADERS, _create_project


@pytest.mark.anyio
async def test_nested_array_cascade_delete(client: AsyncClient):
    """Test CASCADE DELETE with nested array FK (images[*].image_id)."""
    # Setup: Create project
    project_name = "test_nested_array_cascade_delete"
    await _create_project(client, project_name)

    # Create Images context
    images_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Images",
            "description": "Image metadata",
            "unique_keys": {"image_id": "int"},
            "auto_counting": {"image_id": None},
        },
        headers=HEADERS,
    )
    assert images_context_response.status_code == 200

    # Create Transcripts context with nested FK
    transcripts_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Transcripts",
            "description": "Transcripts with nested image references",
            "foreign_keys": [
                {
                    "name": "images[*].image_id",
                    "references": "Images.image_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert transcripts_context_response.status_code == 200

    # Create image logs
    img1_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "entries": {"url": "https://example.com/1.jpg"},
        },
        headers=HEADERS,
    )
    assert img1_response.status_code == 200
    img1_log_id = img1_response.json()["log_event_ids"][0]

    img2_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "entries": {"url": "https://example.com/2.jpg"},
        },
        headers=HEADERS,
    )
    assert img2_response.status_code == 200
    img2_log_id = img2_response.json()["log_event_ids"][0]

    # Get the actual auto-generated image_id values by matching log_event_ids
    img_logs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Images",
        },
        headers=HEADERS,
    )
    assert img_logs_response.status_code == 200
    img_logs = img_logs_response.json()["logs"]

    # Match by id (API doesn't guarantee order)
    img1_id = next(
        log["entries"]["image_id"] for log in img_logs if log["id"] == img1_log_id
    )
    img2_id = next(
        log["entries"]["image_id"] for log in img_logs if log["id"] == img2_log_id
    )

    # Create transcript with multiple images using actual auto-generated IDs
    transcript_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_001",
                "images": [
                    {"image_id": img1_id, "caption": "First image"},
                    {"image_id": img2_id, "caption": "Second image"},
                ],
            },
        },
        headers=HEADERS,
    )
    assert transcript_response.status_code == 200
    transcript_log_id = transcript_response.json()["log_event_ids"][0]

    # Verify transcript exists
    get_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Transcripts",
        },
        headers=HEADERS,
    )
    assert get_response.status_code == 200
    assert len(get_response.json()["logs"]) == 1

    # Delete img_001 - should CASCADE DELETE the transcript
    delete_response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "ids_and_fields": [[img1_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify transcript was cascade deleted
    get_after_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Transcripts",
        },
        headers=HEADERS,
    )
    assert get_after_response.status_code == 200
    assert len(get_after_response.json()["logs"]) == 0

    # Verify img_002 still exists
    get_img2_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Images",
        },
        headers=HEADERS,
    )
    assert get_img2_response.status_code == 200
    results = get_img2_response.json()["logs"]
    assert len(results) == 1
    # Verify it's img2 by matching the id
    assert results[0]["id"] == img2_log_id
    assert results[0]["entries"]["image_id"] == img2_id


@pytest.mark.anyio
async def test_nested_array_cascade_update(client: AsyncClient):
    """Test CASCADE UPDATE with nested array FK (images[*].image_id)."""
    # Setup: Create project
    project_name = "test_nested_array_cascade_update"
    await _create_project(client, project_name)

    # Create Images context (is_versioned allows updates, no unique_keys to avoid immutable field error)
    images_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Images",
            "description": "Image metadata",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert images_context_response.status_code == 200

    # Create Transcripts context with nested FK
    transcripts_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Transcripts",
            "description": "Transcripts with nested image references",
            "is_versioned": True,
            "foreign_keys": [
                {
                    "name": "images[*].image_id",
                    "references": "Images.image_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert transcripts_context_response.status_code == 200

    # Create image
    img_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "entries": {"image_id": 10, "url": "https://example.com/old.jpg"},
        },
        headers=HEADERS,
    )
    assert img_response.status_code == 200
    img_log_id = img_response.json()["log_event_ids"][0]

    # Create transcripts referencing the image
    transcript1_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_001",
                "images": [
                    {"image_id": 10, "caption": "Old ID"},
                ],
            },
        },
        headers=HEADERS,
    )
    assert transcript1_response.status_code == 200

    transcript2_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_002",
                "images": [
                    {"image_id": 10, "caption": "Also old"},
                    {"image_id": 10, "caption": "Duplicate old"},
                ],
            },
        },
        headers=HEADERS,
    )
    assert transcript2_response.status_code == 200

    # Update image_id from 10 to 99
    update_response = await client.put(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "logs": [img_log_id],
            "entries": {"image_id": 99},
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert update_response.status_code == 200

    # Verify all transcripts were cascade updated
    get_transcripts_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Transcripts",
        },
        headers=HEADERS,
    )
    assert get_transcripts_response.status_code == 200
    results = get_transcripts_response.json()["logs"]
    assert len(results) == 2

    # Check transcript 1
    t1 = next(r for r in results if r["entries"]["transcript_id"] == "t_001")
    assert len(t1["entries"]["images"]) == 1
    assert t1["entries"]["images"][0]["image_id"] == 99

    # Check transcript 2 - both occurrences should be updated
    t2 = next(r for r in results if r["entries"]["transcript_id"] == "t_002")
    assert len(t2["entries"]["images"]) == 2
    assert t2["entries"]["images"][0]["image_id"] == 99
    assert t2["entries"]["images"][1]["image_id"] == 99


@pytest.mark.anyio
async def test_nested_array_set_null(client: AsyncClient):
    """Test SET NULL with nested array FK (images[*].image_id)."""
    # Setup: Create project
    project_name = "test_nested_array_set_null"
    await _create_project(client, project_name)

    # Create Images context
    images_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Images",
            "description": "Image metadata",
            "unique_keys": {"image_id": "int"},
            "auto_counting": {"image_id": None},
        },
        headers=HEADERS,
    )
    assert images_context_response.status_code == 200

    # Create Transcripts context with nested FK (SET NULL)
    transcripts_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Transcripts",
            "description": "Transcripts with nested image references",
            "is_versioned": True,
            "foreign_keys": [
                {
                    "name": "images[*].image_id",
                    "references": "Images.image_id",
                    "on_delete": "SET NULL",
                    "on_update": "SET NULL",
                },
            ],
        },
        headers=HEADERS,
    )
    assert transcripts_context_response.status_code == 200

    # Create image
    img_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "entries": {"url": "https://example.com/1.jpg"},
        },
        headers=HEADERS,
    )
    assert img_response.status_code == 200
    img_log_id = img_response.json()["log_event_ids"][0]

    # Get the actual auto-generated image_id value
    img_logs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Images",
        },
        headers=HEADERS,
    )
    assert img_logs_response.status_code == 200
    img_id = img_logs_response.json()["logs"][0]["entries"]["image_id"]

    # Create transcript referencing the image using actual auto-generated ID
    transcript_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_001",
                "images": [
                    {"image_id": img_id, "caption": "First image"},
                ],
            },
        },
        headers=HEADERS,
    )
    assert transcript_response.status_code == 200

    # Delete the image - should SET NULL the nested FK
    delete_response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "ids_and_fields": [[img_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify transcript still exists but image_id is null
    get_transcripts_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Transcripts",
        },
        headers=HEADERS,
    )
    assert get_transcripts_response.status_code == 200
    results = get_transcripts_response.json()["logs"]
    assert len(results) == 1

    # Check that image_id is set to null
    assert results[0]["entries"]["images"][0]["image_id"] is None
    assert results[0]["entries"]["images"][0]["caption"] == "First image"


@pytest.mark.anyio
async def test_nested_object_cascade_delete(client: AsyncClient):
    """Test CASCADE DELETE with nested object FK (metadata.author.user_id)."""
    # Setup: Create project
    project_name = "test_nested_object_cascade_delete"
    await _create_project(client, project_name)

    # Create Users context
    users_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Users",
            "description": "User accounts",
            "unique_keys": {"user_id": "int"},
            "auto_counting": {"user_id": None},
        },
        headers=HEADERS,
    )
    assert users_context_response.status_code == 200

    # Create Documents context with nested object FK
    documents_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Documents",
            "description": "Documents with nested author reference",
            "foreign_keys": [
                {
                    "name": "metadata.author.user_id",
                    "references": "Users.user_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert documents_context_response.status_code == 200

    # Create user
    user_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Users",
            "entries": {"name": "Alice"},
        },
        headers=HEADERS,
    )
    assert user_response.status_code == 200
    user_log_id = user_response.json()["log_event_ids"][0]

    # Get the actual auto-generated user_id value
    user_logs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Users",
        },
        headers=HEADERS,
    )
    assert user_logs_response.status_code == 200
    user_id = user_logs_response.json()["logs"][0]["entries"]["user_id"]

    # Create document referencing the user using actual auto-generated ID
    doc_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Documents",
            "entries": {
                "doc_id": "doc_001",
                "metadata": {
                    "author": {
                        "user_id": user_id,
                        "role": "editor",
                    },
                    "created_at": "2024-01-01",
                },
            },
        },
        headers=HEADERS,
    )
    assert doc_response.status_code == 200

    # Delete user - should CASCADE DELETE the document
    delete_response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Users",
            "ids_and_fields": [[user_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify document was cascade deleted
    get_docs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Documents",
        },
        headers=HEADERS,
    )
    assert get_docs_response.status_code == 200
    assert len(get_docs_response.json()["logs"]) == 0


@pytest.mark.anyio
async def test_nested_object_cascade_update(client: AsyncClient):
    """Test CASCADE UPDATE with nested object FK (metadata.author.user_id)."""
    # Setup: Create project
    project_name = "test_nested_object_cascade_update"
    await _create_project(client, project_name)

    # Create Users context (is_versioned allows updates, no unique_keys to avoid immutable field error)
    users_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Users",
            "description": "User accounts",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert users_context_response.status_code == 200

    # Create Documents context with nested object FK
    documents_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Documents",
            "description": "Documents with nested author reference",
            "is_versioned": True,
            "foreign_keys": [
                {
                    "name": "metadata.author.user_id",
                    "references": "Users.user_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert documents_context_response.status_code == 200

    # Create user
    user_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Users",
            "entries": {"user_id": 100, "name": "Alice"},
        },
        headers=HEADERS,
    )
    assert user_response.status_code == 200
    user_log_id = user_response.json()["log_event_ids"][0]

    # Create document referencing the user
    doc_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Documents",
            "entries": {
                "doc_id": "doc_001",
                "metadata": {
                    "author": {
                        "user_id": 100,
                        "role": "editor",
                    },
                    "created_at": "2024-01-01",
                },
            },
        },
        headers=HEADERS,
    )
    assert doc_response.status_code == 200

    # Update user_id from 100 to 999
    update_response = await client.put(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Users",
            "logs": [user_log_id],
            "entries": {"user_id": 999},
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert update_response.status_code == 200

    # Verify document was cascade updated
    get_docs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Documents",
        },
        headers=HEADERS,
    )
    assert get_docs_response.status_code == 200
    results = get_docs_response.json()["logs"]
    assert len(results) == 1
    assert results[0]["entries"]["metadata"]["author"]["user_id"] == 999
    assert results[0]["entries"]["metadata"]["author"]["role"] == "editor"


@pytest.mark.anyio
async def test_deeply_nested_cascade_delete(client: AsyncClient):
    """Test CASCADE DELETE with deeply nested FK (teams[*].members[*].user_id)."""
    # Setup: Create project
    project_name = "test_deeply_nested_cascade_delete"
    await _create_project(client, project_name)

    # Create Users context
    users_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Users",
            "description": "User accounts",
            "unique_keys": {"user_id": "int"},
            "auto_counting": {"user_id": None},
        },
        headers=HEADERS,
    )
    assert users_context_response.status_code == 200

    # Create Organizations context with deeply nested FK
    orgs_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Organizations",
            "description": "Organizations with nested team structure",
            "foreign_keys": [
                {
                    "name": "teams[*].members[*].user_id",
                    "references": "Users.user_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert orgs_context_response.status_code == 200

    # Create users
    user1_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Users",
            "entries": {"name": "Alice"},
        },
        headers=HEADERS,
    )
    assert user1_response.status_code == 200
    user1_log_id = user1_response.json()["log_event_ids"][0]

    user2_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Users",
            "entries": {"name": "Bob"},
        },
        headers=HEADERS,
    )
    assert user2_response.status_code == 200
    user2_log_id = user2_response.json()["log_event_ids"][0]

    # Get the actual auto-generated user_id values by matching log_event_ids
    user_logs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Users",
        },
        headers=HEADERS,
    )
    assert user_logs_response.status_code == 200
    user_logs = user_logs_response.json()["logs"]

    # Match by id (API doesn't guarantee order)
    user1_id = next(
        log["entries"]["user_id"] for log in user_logs if log["id"] == user1_log_id
    )
    user2_id = next(
        log["entries"]["user_id"] for log in user_logs if log["id"] == user2_log_id
    )

    # Create organization with nested team structure using actual auto-generated IDs
    org_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Organizations",
            "entries": {
                "org_id": "org_001",
                "teams": [
                    {
                        "team_name": "Engineering",
                        "members": [
                            {"user_id": user1_id, "role": "lead"},
                            {"user_id": user2_id, "role": "developer"},
                        ],
                    },
                    {
                        "team_name": "Design",
                        "members": [
                            {"user_id": user1_id, "role": "designer"},
                        ],
                    },
                ],
            },
        },
        headers=HEADERS,
    )
    assert org_response.status_code == 200

    # Delete user_001 - should CASCADE DELETE the organization
    delete_response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Users",
            "ids_and_fields": [[user1_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify organization was cascade deleted
    get_orgs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Organizations",
        },
        headers=HEADERS,
    )
    assert get_orgs_response.status_code == 200
    assert len(get_orgs_response.json()["logs"]) == 0


@pytest.mark.anyio
async def test_nested_fk_multiple_occurrences_update(client: AsyncClient):
    """Test CASCADE UPDATE updates ALL occurrences in nested arrays."""
    # Setup: Create project
    project_name = "test_nested_fk_multiple_occurrences"
    await _create_project(client, project_name)

    # Create Tags context (is_versioned allows updates, no unique_keys to avoid immutable field error)
    tags_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Tags",
            "description": "Tag definitions",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert tags_context_response.status_code == 200

    # Create Posts context with nested FK
    posts_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Posts",
            "description": "Blog posts with tags",
            "is_versioned": True,
            "foreign_keys": [
                {
                    "name": "tags[*].tag_id",
                    "references": "Tags.tag_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert posts_context_response.status_code == 200

    # Create tag
    tag_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Tags",
            "entries": {"tag_id": 50, "label": "Python"},
        },
        headers=HEADERS,
    )
    assert tag_response.status_code == 200
    tag_log_id = tag_response.json()["log_event_ids"][0]

    # Create post with SAME tag appearing multiple times
    post_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Posts",
            "entries": {
                "post_id": "post_001",
                "tags": [
                    {"tag_id": 50, "weight": 1.0},
                    {"tag_id": 50, "weight": 0.8},
                    {"tag_id": 50, "weight": 0.6},
                ],
            },
        },
        headers=HEADERS,
    )
    assert post_response.status_code == 200

    # Update tag_id
    update_response = await client.put(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Tags",
            "logs": [tag_log_id],
            "entries": {"tag_id": 99},
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert update_response.status_code == 200

    # Verify ALL occurrences were updated
    get_posts_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Posts",
        },
        headers=HEADERS,
    )
    assert get_posts_response.status_code == 200
    results = get_posts_response.json()["logs"]
    assert len(results) == 1

    # All three tags should be updated
    tags = results[0]["entries"]["tags"]
    assert len(tags) == 3
    assert all(tag["tag_id"] == 99 for tag in tags)
    assert tags[0]["weight"] == 1.0
    assert tags[1]["weight"] == 0.8
    assert tags[2]["weight"] == 0.6


@pytest.mark.anyio
async def test_nested_fk_no_cascade_if_value_not_present(client: AsyncClient):
    """Test that CASCADE doesn't affect logs without the nested FK value."""
    # Setup: Create project
    project_name = "test_nested_fk_no_cascade"
    await _create_project(client, project_name)

    # Create Images context
    images_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Images",
            "description": "Image metadata",
            "unique_keys": {"image_id": "int"},
            "auto_counting": {"image_id": None},
        },
        headers=HEADERS,
    )
    assert images_context_response.status_code == 200

    # Create Transcripts context with nested FK
    transcripts_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Transcripts",
            "description": "Transcripts with optional image references",
            "foreign_keys": [
                {
                    "name": "images[*].image_id",
                    "references": "Images.image_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert transcripts_context_response.status_code == 200

    # Create images with auto_counting
    img1_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "entries": {"url": "https://example.com/1.jpg"},
        },
        headers=HEADERS,
    )
    assert img1_response.status_code == 200
    img1_log_id = img1_response.json()["log_event_ids"][0]

    img2_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "entries": {"url": "https://example.com/2.jpg"},
        },
        headers=HEADERS,
    )
    assert img2_response.status_code == 200
    img2_log_id = img2_response.json()["log_event_ids"][0]

    # Get the actual auto-generated image_id values by matching log_event_ids
    img_logs_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Images",
        },
        headers=HEADERS,
    )
    assert img_logs_response.status_code == 200
    img_logs = img_logs_response.json()["logs"]

    # Match by id (API doesn't guarantee order)
    img1_id = next(
        log["entries"]["image_id"] for log in img_logs if log["id"] == img1_log_id
    )
    img2_id = next(
        log["entries"]["image_id"] for log in img_logs if log["id"] == img2_log_id
    )

    # Create transcript 1 referencing first image using actual auto-generated ID
    t1_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_001",
                "images": [
                    {"image_id": img1_id, "caption": "First"},
                ],
            },
        },
        headers=HEADERS,
    )
    assert t1_response.status_code == 200

    # Create transcript 2 referencing second image using actual auto-generated ID
    t2_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_002",
                "images": [
                    {"image_id": img2_id, "caption": "Second"},
                ],
            },
        },
        headers=HEADERS,
    )
    assert t2_response.status_code == 200

    # Create transcript 3 with NO images field
    t3_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_003",
                "text": "No images here",
            },
        },
        headers=HEADERS,
    )
    assert t3_response.status_code == 200

    # Delete img_001 - should only cascade delete t_001
    delete_response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Images",
            "ids_and_fields": [[img1_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify only t_001 was deleted
    get_transcripts_response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "Transcripts",
        },
        headers=HEADERS,
    )
    assert get_transcripts_response.status_code == 200
    results = get_transcripts_response.json()["logs"]
    assert len(results) == 2

    transcript_ids = {r["entries"]["transcript_id"] for r in results}
    assert transcript_ids == {"t_002", "t_003"}


@pytest.mark.anyio
async def test_nested_fk_validation_prevents_invalid_reference(client: AsyncClient):
    """Test that nested FK validation prevents creating logs with invalid references."""
    # Setup: Create project
    project_name = "test_nested_fk_validation"
    await _create_project(client, project_name)

    # Create Images context
    images_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Images",
            "description": "Image metadata",
            "unique_keys": {"image_id": "int"},
            "auto_counting": {"image_id": None},
        },
        headers=HEADERS,
    )
    assert images_context_response.status_code == 200

    # Create Transcripts context with nested FK
    transcripts_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Transcripts",
            "description": "Transcripts with nested image references",
            "foreign_keys": [
                {
                    "name": "images[*].image_id",
                    "references": "Images.image_id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert transcripts_context_response.status_code == 200

    # Try to create transcript with non-existent image_id - should fail with 400
    transcript_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Transcripts",
            "entries": {
                "transcript_id": "t_001",
                "images": [
                    {"image_id": 999, "caption": "Invalid"},
                ],
            },
        },
        headers=HEADERS,
    )
    # Should get 400 Bad Request due to FK constraint violation
    assert transcript_response.status_code == 400
    error_detail = transcript_response.json()["detail"]
    assert "foreign key constraint violation" in error_detail.lower()
    assert "images[*].image_id" in error_detail
    assert "999" in error_detail


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
