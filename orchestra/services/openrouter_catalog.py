"""OpenRouter model catalog for assistant default/slow-brain selection.

Fetches ``GET /api/v1/models``, caches a TTL snapshot, and exposes capability
filters used by the assistant model picker (multimodal image input required).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from orchestra.settings import settings

_LOGGER = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_DEFAULT_TTL_SECONDS = 6 * 60 * 60

_lock = threading.RLock()
_snapshot: dict[str, Any] | None = None
_snapshot_fetched_at: float = 0.0


def _api_key() -> str:
    return (settings.openrouter_api_key or "").strip()


def _normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    pricing = raw.get("pricing") or {}
    architecture = raw.get("architecture") or {}
    input_modalities = architecture.get("input_modalities") or raw.get(
        "input_modalities",
    )
    if not isinstance(input_modalities, list):
        input_modalities = []
    modalities = {str(m).lower() for m in input_modalities}
    supported_params = raw.get("supported_parameters") or []
    if not isinstance(supported_params, list):
        supported_params = []
    supported_l = {str(p).lower() for p in supported_params}

    def _f(key: str) -> float | None:
        value = pricing.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "context_length": raw.get("context_length"),
        "input_cost_per_token": _f("prompt"),
        "output_cost_per_token": _f("completion"),
        "input_modalities": sorted(modalities),
        "supports_image_input": "image" in modalities,
        "supports_tools": bool(
            {"tools", "tool_choice", "functions", "function_call"} & supported_l,
        ),
        "supports_reasoning": bool(
            {"reasoning", "include_reasoning", "reasoning_effort"} & supported_l,
        ),
    }


def _fetch() -> dict[str, Any] | None:
    headers = {"Accept": "application/json", "User-Agent": "orchestra"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(_OPENROUTER_MODELS_URL, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        _LOGGER.warning("Failed to fetch OpenRouter model catalog", exc_info=True)
        return None

    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return None
    models: dict[str, dict[str, Any]] = {}
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        normalized = _normalize(entry)
        if normalized is None:
            continue
        models[normalized["id"]] = normalized
    return {
        "fetched_at": time.time(),
        "models": models,
    }


def get_catalog(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Return ``{openrouter_id: metadata}`` from memory or remote fetch."""

    global _snapshot, _snapshot_fetched_at
    with _lock:
        now = time.time()
        if (
            not refresh
            and _snapshot is not None
            and now - _snapshot_fetched_at < _DEFAULT_TTL_SECONDS
        ):
            return dict(_snapshot.get("models") or {})
        remote = _fetch()
        if remote is not None:
            _snapshot = remote
            _snapshot_fetched_at = float(remote["fetched_at"])
            return dict(_snapshot.get("models") or {})
        if _snapshot is not None:
            return dict(_snapshot.get("models") or {})
        return {}


def openrouter_endpoint(model_id: str) -> str:
    return f"{model_id}@openrouter"


def parse_openrouter_endpoint(endpoint: str) -> Optional[str]:
    if not endpoint.endswith("@openrouter"):
        return None
    model_id = endpoint[: -len("@openrouter")]
    return model_id or None


def get_model(model_id: str) -> dict[str, Any] | None:
    catalog = get_catalog()
    info = catalog.get(model_id)
    if info is not None:
        return info
    catalog = get_catalog(refresh=True)
    return catalog.get(model_id)


def is_eligible_assistant_model(
    endpoint: str,
    *,
    require_tools: bool = False,
) -> tuple[bool, str | None]:
    """Return (ok, disabled_reason) for assistant actor/slow-brain selection."""

    model_id = parse_openrouter_endpoint(endpoint)
    if model_id is None:
        return False, "Only *@openrouter endpoints are accepted from the OpenRouter catalog."
    info = get_model(model_id)
    if info is None:
        return False, f"Unknown OpenRouter model {model_id!r}."
    if not info.get("supports_image_input"):
        return False, "Model does not support native image input (required)."
    if require_tools and not info.get("supports_tools"):
        return False, "Model does not support tool calling (required for task model)."
    return True, None


def search_models(
    query: str,
    *,
    limit: int = 50,
    require_tools: bool = False,
) -> list[dict[str, Any]]:
    """Search catalog models; incompatible rows included with disabled_reason."""

    q = query.strip().lower()
    catalog = get_catalog()
    results: list[dict[str, Any]] = []
    for model_id, info in catalog.items():
        hay = f"{model_id} {info.get('name') or ''}".lower()
        if q and q not in hay:
            continue
        endpoint = openrouter_endpoint(model_id)
        ok, reason = True, None
        if not info.get("supports_image_input"):
            ok, reason = False, "No native image input"
        elif require_tools and not info.get("supports_tools"):
            ok, reason = False, "No tool calling"
        results.append(
            {
                **info,
                "endpoint": endpoint,
                "eligible": ok,
                "disabled_reason": reason,
            },
        )
    results.sort(key=lambda row: (not row["eligible"], row["id"]))
    return results[: max(1, min(limit, 200))]
