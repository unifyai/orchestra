"""Ed25519 keypairs for on-demand user-home filesystem access.

Each ``(assistant, user-desktop)`` link with filesystem sync enabled gets its
own keypair. Orchestra holds the private key; only the derived public key is
installed in the device's app-owned ``authorized_keys``.
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

_KEY_COMMENT = "unity-user-filesync"


def _public_openssh(key: ed25519.Ed25519PrivateKey) -> str:
    pub = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    return f"{pub} {_KEY_COMMENT}"


def generate_filesync_keypair() -> tuple[str, str]:
    """Return ``(private_openssh_pem, public_openssh)`` for a fresh keypair."""
    key = ed25519.Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private_pem, _public_openssh(key)


def public_from_private(private_pem: str) -> str:
    """Derive the OpenSSH public key from a stored private PEM."""
    key = serialization.load_ssh_private_key(private_pem.encode(), password=None)
    return _public_openssh(key)
