"""Back-compat re-exports for Builtins guards.

Prefer :mod:`orchestra.web.api.utils.system_project` for new call sites.
"""

from orchestra.web.api.utils.system_project import (
    BUILTINS_PROJECT_NAME,
    is_builtins_project_name,
    reject_builtins_project_operation,
    reject_system_project_operation,
    require_builtins_project_owner,
    require_system_project_owner,
)

__all__ = [
    "BUILTINS_PROJECT_NAME",
    "is_builtins_project_name",
    "reject_builtins_project_operation",
    "reject_system_project_operation",
    "require_builtins_project_owner",
    "require_system_project_owner",
]
