"""Guards and name helpers for platform-owned ``is_system`` projects.

System projects have no ``user_id`` / ``organization_id``. Writes are allowed
only when the caller is the Orchestra system principal (``user_id ==
"__system__"``, typically ``ORCHESTRA_ADMIN_KEY`` on the data plane).

* **Builtins** — public catalogue: any caller may resolve/read; only
  ``__system__`` may write.
* **AssistantJobs** — private fleet audit: only ``__system__`` may resolve,
  read, or write.
"""

from __future__ import annotations

from fastapi import HTTPException, status

BUILTINS_PROJECT_NAME = "Builtins"
ASSISTANT_JOBS_PROJECT_NAME = "AssistantJobs"

# Resolved for every authenticated caller (public catalogue read path).
PUBLIC_SYSTEM_PROJECT_NAMES = frozenset({BUILTINS_PROJECT_NAME})

# Resolved only for the system principal (fleet / infra audit).
PRIVATE_SYSTEM_PROJECT_NAMES = frozenset({ASSISTANT_JOBS_PROJECT_NAME})

SYSTEM_PROJECT_NAMES = PUBLIC_SYSTEM_PROJECT_NAMES | PRIVATE_SYSTEM_PROJECT_NAMES


def is_system_project_name(project_name: str | None) -> bool:
    """Return True for a reserved platform system project name."""
    return project_name in SYSTEM_PROJECT_NAMES


def is_public_system_project_name(project_name: str | None) -> bool:
    """Return True for system projects that resolve for any caller."""
    return project_name in PUBLIC_SYSTEM_PROJECT_NAMES


def is_private_system_project_name(project_name: str | None) -> bool:
    """Return True for system projects that resolve only for ``__system__``."""
    return project_name in PRIVATE_SYSTEM_PROJECT_NAMES


def is_builtins_project_name(project_name: str | None) -> bool:
    """Return True for the reserved Builtins project name."""
    return project_name == BUILTINS_PROJECT_NAME


def require_system_project_owner(
    project,
    *,
    user_id: str,
    action: str = "modify",
) -> None:
    """Allow mutations on a system project only through the platform writer."""
    name = getattr(project, "name", None) or "system"
    if getattr(project, "is_system", False) and user_id == "__system__":
        return
    if getattr(project, "is_system", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"The '{name}' project is reserved and cannot be "
                f"{action} by user API keys."
            ),
        )
    if project.user_id == user_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"The '{name}' project is reserved and cannot be "
            f"{action} outside its owning principal."
        ),
    )


def reject_system_project_operation(
    project_name: str | None,
    *,
    action: str,
) -> None:
    """Reject destructive whole-project operations against system projects."""
    if not is_system_project_name(project_name):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"The '{project_name}' project is reserved and cannot be {action}.",
    )


# Back-compat aliases used by Builtins-era call sites.
require_builtins_project_owner = require_system_project_owner
reject_builtins_project_operation = reject_system_project_operation
