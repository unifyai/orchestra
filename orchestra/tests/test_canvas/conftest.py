"""Shared helpers for canvas token tests."""


def token_body(
    token: str,
    context_name: str,
    project_name: str,
    **overrides,
) -> dict:
    """Build a RegisterCanvasTokenRequest JSON body."""
    body = {
        "token": token,
        "context_name": context_name,
        "project_name": project_name,
    }
    body.update(overrides)
    return body
