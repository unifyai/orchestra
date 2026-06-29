"""Guards for the reserved platform Builtins project."""

from fastapi import HTTPException, status

BUILTINS_PROJECT_NAME = "Builtins"


def is_builtins_project_name(project_name: str | None) -> bool:
    """Return True for the reserved Builtins project name."""
    return project_name == BUILTINS_PROJECT_NAME


def require_builtins_project_owner(
    project,
    *,
    user_id: str,
    action: str = "modify",
) -> None:
    """Allow Builtins catalogue convergence only through a platform writer."""
    if getattr(project, "is_system", False) and user_id == "__system__":
        return
    if getattr(project, "is_system", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"The '{BUILTINS_PROJECT_NAME}' project is reserved and cannot be "
                f"{action} by user API keys."
            ),
        )
    if project.user_id == user_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"The '{BUILTINS_PROJECT_NAME}' project is reserved and cannot be "
            f"{action} outside its owning principal."
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
