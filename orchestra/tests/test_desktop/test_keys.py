"""Unit tests for the user-filesync keypair helpers (no DB)."""

from cryptography.hazmat.primitives import serialization

from orchestra.web.api.desktop.keys import (
    generate_filesync_keypair,
    public_from_private,
)


def test_generate_filesync_keypair_roundtrip():
    private_pem, public = generate_filesync_keypair()

    # Public key is a commented OpenSSH ed25519 key.
    assert public.startswith("ssh-ed25519 ")
    assert public.endswith("unity-user-filesync")

    # Private key is a loadable OpenSSH private key.
    loaded = serialization.load_ssh_private_key(private_pem.encode(), password=None)
    assert loaded is not None


def test_public_from_private_matches():
    private_pem, public = generate_filesync_keypair()
    assert public_from_private(private_pem) == public


def test_keypairs_are_unique():
    priv_a, pub_a = generate_filesync_keypair()
    priv_b, pub_b = generate_filesync_keypair()
    assert priv_a != priv_b
    assert pub_a != pub_b
