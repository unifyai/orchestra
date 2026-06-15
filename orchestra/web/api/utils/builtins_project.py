"""Guards for the reserved platform Builtins project."""

from typing import Optional

from fastapi import HTTPException, status

BUILTINS_PROJECT_NAME = "Builtins"


def is_builtins_project_name(project_name: str | None) -> bool:
    """Return True for the reserved Builtins project name."""
    return project_name == BUILTINS_PROJECT_NAME


def require_builtins_org_writer(
    *,
    organization_id: Optional[int],
    action: str = "modify",
) -> None:
    """Reject Builtins writes from personal API keys."""
    if organization_id is not None:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"The '{BUILTINS_PROJECT_NAME}' project is reserved and cannot be "
            f"{action} from a personal workspace."
        ),
    )


def reject_builtins_project_operation(
    project_name: str | None,
    *,
    action: str,
) -> None:
    """Reject destructive whole-project operations against Builtins."""
    if not is_builtins_project_name(project_name):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"The '{BUILTINS_PROJECT_NAME}' project is reserved and cannot be {action}.",
    )
