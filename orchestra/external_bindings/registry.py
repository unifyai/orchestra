"""Connector registry for external field bindings."""

from __future__ import annotations

from typing import Dict

from orchestra.external_bindings.http_generic import HttpGenericConnector
from orchestra.external_bindings.types import ExternalConnector

_REGISTRY: Dict[str, ExternalConnector] = {}


def register_connector(connector: ExternalConnector) -> None:
    _REGISTRY[connector.id] = connector


def get_connector(connector_id: str) -> ExternalConnector:
    if connector_id not in _REGISTRY:
        raise KeyError(f"Unknown external connector: {connector_id}")
    return _REGISTRY[connector_id]


def list_connectors() -> list[str]:
    return sorted(_REGISTRY)


def _ensure_builtins() -> None:
    if "http.generic" not in _REGISTRY:
        register_connector(HttpGenericConnector())


_ensure_builtins()
