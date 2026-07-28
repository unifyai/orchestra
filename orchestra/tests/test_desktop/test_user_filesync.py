"""On-demand user-home filesystem access: per-link keys + SFTP tunnel coords.

Covers the Orchestra contract the CM relies on: a per-(assistant, user-desktop)
keypair minted when filesystem sync is enabled, the device-facing pubkey
endpoint, the device-reported SFTP tunnel coordinates, and the strict isolation
that keeps private keys out of the user-facing (pod-bound) assistant read.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls():
    with (
        patch(
            "orchestra.web.api.assistant.views.wake_up_assistant",
            new_callable=AsyncMock,
        ) as mock_wake_up,
        patch(
            "orchestra.web.api.assistant.views.reawaken_assistant",
            new_callable=AsyncMock,
        ) as mock_reawaken,
    ):
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield


# =============================================================================
# Helpers
# =============================================================================


async def _create_desktop(
    client: AsyncClient,
    url: str = "https://fs.tunnel.unify.ai",
    os_name: str = "macos",
) -> int:
    resp = await client.post(
        "/v0/desktop",
        json={"name": "FS Desktop", "url": url, "os": os_name},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["info"]["id"]


async def _create_assistant(client: AsyncClient, first_name: str = "Fs") -> str:
    resp = await client.post(
        "/v0/assistant",
        json={"first_name": first_name, "surname": "Bot", "create_infra": False},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["info"]["agent_id"]


async def _link(
    client: AsyncClient,
    agent_id: str,
    desktop_id: int,
    filesys_sync: bool,
) -> dict:
    resp = await client.post(
        "/v0/desktop/link",
        json={
            "assistant_id": int(agent_id),
            "desktop_id": desktop_id,
            "filesys_sync": filesys_sync,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["info"]


async def _setup(client: AsyncClient, filesys_sync: bool = True):
    desktop_id = await _create_desktop(client)
    agent_id = await _create_assistant(client)
    link = await _link(client, agent_id, desktop_id, filesys_sync)
    return agent_id, desktop_id, link


async def _pubkey_resp(client: AsyncClient, agent_id: str):
    return await client.get(
        f"/v0/desktop/link/{agent_id}/pubkey",
        headers=HEADERS,
    )


async def _admin_assistant(client: AsyncClient, agent_id: str) -> dict:
    resp = await client.get(
        f"/v0/admin/assistant?agent_id={int(agent_id)}",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    items = [a for a in resp.json()["info"] if str(a["agent_id"]) == str(agent_id)]
    assert len(items) == 1
    return items[0]


# =============================================================================
# Pubkey endpoint
# =============================================================================


@pytest.mark.anyio
async def test_pubkey_returned_when_filesync_enabled(client: AsyncClient):
    agent_id, _, _ = await _setup(client, filesys_sync=True)
    resp = await _pubkey_resp(client, agent_id)
    assert resp.status_code == 200, resp.json()
    pub = resp.json()["info"]["public_key"]
    assert pub.startswith("ssh-ed25519 ")
    assert "unity-user-filesync" in pub


@pytest.mark.anyio
async def test_pubkey_stable_across_reads(client: AsyncClient):
    agent_id, _, _ = await _setup(client, filesys_sync=True)
    r1 = await _pubkey_resp(client, agent_id)
    r2 = await _pubkey_resp(client, agent_id)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["info"]["public_key"] == r2.json()["info"]["public_key"]


@pytest.mark.anyio
async def test_pubkey_404_when_filesync_disabled(client: AsyncClient):
    agent_id, _, _ = await _setup(client, filesys_sync=False)
    resp = await _pubkey_resp(client, agent_id)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_pubkey_404_when_no_link(client: AsyncClient):
    agent_id = await _create_assistant(client)
    resp = await _pubkey_resp(client, agent_id)
    assert resp.status_code == 404


# =============================================================================
# Key lifecycle on filesys_sync toggle
# =============================================================================


@pytest.mark.anyio
async def test_disabling_filesync_clears_key(client: AsyncClient):
    agent_id, desktop_id, _ = await _setup(client, filesys_sync=True)
    assert (await _pubkey_resp(client, agent_id)).status_code == 200

    await _link(client, agent_id, desktop_id, filesys_sync=False)
    assert (await _pubkey_resp(client, agent_id)).status_code == 404


@pytest.mark.anyio
async def test_reenabling_filesync_mints_new_key(client: AsyncClient):
    agent_id, desktop_id, _ = await _setup(client, filesys_sync=True)
    p1 = (await _pubkey_resp(client, agent_id)).json()["info"]["public_key"]

    await _link(client, agent_id, desktop_id, filesys_sync=False)
    await _link(client, agent_id, desktop_id, filesys_sync=True)
    p2 = (await _pubkey_resp(client, agent_id)).json()["info"]["public_key"]

    assert p1 != p2


# =============================================================================
# SFTP tunnel reporting
# =============================================================================


@pytest.mark.anyio
async def test_set_sftp_tunnel_records_coords(client: AsyncClient):
    agent_id, _, _ = await _setup(client, filesys_sync=True)
    resp = await client.post(
        f"/v0/desktop/link/{agent_id}/sftp-tunnel",
        json={"host": "tunnel.unify.ai", "port": 61005},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()


@pytest.mark.anyio
async def test_set_sftp_tunnel_404_when_no_link(client: AsyncClient):
    agent_id = await _create_assistant(client)
    resp = await client.post(
        f"/v0/desktop/link/{agent_id}/sftp-tunnel",
        json={"host": "tunnel.unify.ai", "port": 61005},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# =============================================================================
# Propagation onto the assistant read (the CM contract)
# =============================================================================


@pytest.mark.anyio
async def test_admin_read_exposes_tunnel_coords(client: AsyncClient):
    agent_id, _, link = await _setup(client, filesys_sync=True)
    await client.post(
        f"/v0/desktop/link/{agent_id}/sftp-tunnel",
        json={"host": "tunnel.unify.ai", "port": 61007},
        headers=HEADERS,
    )

    a = await _admin_assistant(client, agent_id)
    owner = link["owner_user_id"]
    entry = [d for d in a["user_desktops"] if d["owner_user_id"] == owner][0]
    assert entry["sftp_tunnel_host"] == "tunnel.unify.ai"
    assert entry["sftp_tunnel_port"] == 61007


@pytest.mark.anyio
async def test_admin_read_exposes_private_key(client: AsyncClient):
    agent_id, _, link = await _setup(client, filesys_sync=True)
    owner = link["owner_user_id"]

    a = await _admin_assistant(client, agent_id)
    keys = a["user_desktop_filesync_keys"]
    assert owner in keys

    # The private key is loadable and its derived public matches the device key.
    loaded = serialization.load_ssh_private_key(keys[owner].encode(), password=None)
    derived = (
        loaded.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    pub = (await _pubkey_resp(client, agent_id)).json()["info"]["public_key"]
    assert pub.startswith(derived)


@pytest.mark.anyio
async def test_user_read_omits_private_keys(client: AsyncClient):
    """The user-facing read must never carry private key material to the pod."""
    agent_id, _, _ = await _setup(client, filesys_sync=True)
    await client.post(
        f"/v0/desktop/link/{agent_id}/sftp-tunnel",
        json={"host": "tunnel.unify.ai", "port": 61009},
        headers=HEADERS,
    )

    resp = await client.get("/v0/assistant", headers=HEADERS)
    assert resp.status_code == 200
    a = [x for x in resp.json()["info"] if str(x["agent_id"]) == str(agent_id)][0]

    assert a.get("user_desktop_filesync_keys", {}) == {}
    assert "PRIVATE KEY" not in str(a.get("user_desktops", []))


# =============================================================================
# Migration sanity (new columns are live in the test DB)
# =============================================================================


@pytest.mark.anyio
async def test_filesync_link_roundtrip(client: AsyncClient):
    _, _, link = await _setup(client, filesys_sync=True)
    assert link["filesys_sync"] is True
