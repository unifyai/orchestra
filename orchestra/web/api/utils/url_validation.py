"""Validation utilities for user-supplied URLs to prevent SSRF attacks."""

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.google.com",
}


def validate_url_for_ssrf(url: str) -> None:
    """
    Validate that a URL is safe to fetch server-side.

    Blocks private/reserved IPs, link-local addresses, loopback,
    and known cloud metadata endpoints.

    Raises HTTPException(400) if the URL is unsafe.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="Only http and https URLs are allowed",
        )

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL: missing hostname")

    if hostname in _BLOCKED_HOSTNAMES:
        raise HTTPException(
            status_code=400,
            detail="Access to this host is not allowed",
        )

    try:
        resolved = socket.getaddrinfo(
            hostname,
            None,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve hostname")

    for family, _type, _proto, _canonname, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
            raise HTTPException(
                status_code=400,
                detail="Access to private/internal network addresses is not allowed",
            )
