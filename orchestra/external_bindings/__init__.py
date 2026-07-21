"""External field bindings: lazy REST-backed Orchestra columns.

See ``docs/external-field-bindings.md`` for the contract. This package is the
server-side hydrate planner and connector registry; UniSDK/Unify only pass
hydrate options and binding metadata.
"""

from orchestra.external_bindings.planner import (
    EXT_SIDECAR_PREFIX,
    HydrateMode,
    hydrate_logs,
    public_binding_summary,
)
from orchestra.external_bindings.registry import (
    get_connector,
    list_connectors,
    register_connector,
)
from orchestra.external_bindings.types import (
    BindingItem,
    BindingResult,
    ConnectorAuth,
    ExternalConnector,
)

__all__ = [
    "EXT_SIDECAR_PREFIX",
    "BindingItem",
    "BindingResult",
    "ConnectorAuth",
    "ExternalConnector",
    "HydrateMode",
    "get_connector",
    "hydrate_logs",
    "list_connectors",
    "public_binding_summary",
    "register_connector",
]
