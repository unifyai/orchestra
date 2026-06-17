"""Resumable Builtins integration catalogue materialization.

The provider app/tool catalog is durable in the Builtins project contexts. The
legacy DB catalog tables can still be updated through the older admin sync API,
but deployment bootstrap should use this module so retries skip completed work
before provider fetch and write Builtins rows with unique-key upserts.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dao.unique_constraint_dao import (
    COMPOSITE_KEY_FIELD,
    UniqueConstraintDAO,
)
from orchestra.db.models.core_models import (
    Context,
    LogEventContext,
    LogUniqueConstraint,
    Project,
)
from orchestra.web.api.integrations.schema import IntegrationCatalogSyncRequest

logger = logging.getLogger(__name__)

BUILTINS_PROJECT_NAME = "Builtins"
BUILTINS_INTEGRATION_APPS_CONTEXT = "Integrations/Apps"
BUILTINS_INTEGRATION_TOOLS_CONTEXT = "Integrations/Tools"
BUILTINS_INTEGRATION_META_CONTEXT = "Integrations/Meta"
DEFAULT_BATCH_SIZE = 25
DEFAULT_WORKERS = 4


@dataclass(frozen=True)
class ProviderCatalogFetchResult:
    apps: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    skipped_apps: list[dict[str, Any]]
    requested_app_slugs: list[str]
    matched_app_slugs: list[str]
    sync_mode: str
    cache_version: str
    auth_configs_created: int = 0
    auth_configs_reused: int = 0
    warning: str | None = None


@dataclass(frozen=True)
class BuiltinsSyncRequest:
    backend_id: str
    environment: str
    desired_hash: str
    cache_version: str
    mode: str = "all"
    app_slugs: list[str] = field(default_factory=list)
    prune_unlisted_apps: bool = False
    checkpoint_key: str | None = None
    sync_payload: dict[str, Any] = field(default_factory=dict)
    desired_config: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_uri: str | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    workers: int = DEFAULT_WORKERS

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BuiltinsSyncRequest":
        sync_payload = dict(payload.get("sync_payload") or {})
        backend_id = str(payload.get("backend_id") or sync_payload.get("backend_id"))
        environment = str(payload.get("environment") or "selfhost")
        desired_hash = str(
            payload.get("desired_hash") or sync_payload.get("cache_version") or "",
        )
        cache_version = str(
            payload.get("cache_version")
            or sync_payload.get("cache_version")
            or desired_hash
            or "builtins-sync-v1",
        )
        if not backend_id:
            raise ValueError("backend_id is required")
        if not desired_hash:
            raise ValueError("desired_hash is required")
        app_slugs = [
            str(slug)
            for slug in (
                payload.get("app_slugs") or sync_payload.get("app_slugs") or []
            )
            if str(slug).strip()
        ]
        return cls(
            backend_id=backend_id,
            environment=environment,
            desired_hash=desired_hash,
            cache_version=cache_version,
            mode=str(payload.get("mode") or "all"),
            app_slugs=app_slugs,
            prune_unlisted_apps=bool(payload.get("prune_unlisted_apps", False)),
            checkpoint_key=(
                str(payload["checkpoint_key"])
                if payload.get("checkpoint_key")
                else None
            ),
            sync_payload=sync_payload,
            desired_config=dict(payload.get("desired_config") or {}),
            run_id=str(payload.get("run_id") or uuid.uuid4()),
            request_uri=(
                str(payload["request_uri"]) if payload.get("request_uri") else None
            ),
            batch_size=max(
                1,
                int(
                    payload.get("batch_size")
                    or os.getenv(
                        "ORCHESTRA_BUILTINS_SYNC_BATCH_SIZE",
                        DEFAULT_BATCH_SIZE,
                    ),
                ),
            ),
            workers=max(
                1,
                int(
                    payload.get("workers")
                    or os.getenv("ORCHESTRA_BUILTINS_SYNC_WORKERS", DEFAULT_WORKERS),
                ),
            ),
        )


@dataclass
class BuiltinsSyncResult:
    status: str
    apps_upserted: int = 0
    tools_upserted: int = 0
    apps_inserted: int = 0
    apps_updated: int = 0
    tools_inserted: int = 0
    tools_updated: int = 0
    apps_pruned: int = 0
    tools_pruned: int = 0
    skipped_batches: int = 0
    completed_batches: int = 0
    matched_app_slugs: list[str] = field(default_factory=list)
    skipped_apps: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    run_id: str | None = None
    desired_hash: str | None = None
    request_uri: str | None = None
    elapsed_seconds: float | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "desired_hash": self.desired_hash,
            "request_uri": self.request_uri,
            "apps_upserted": self.apps_upserted,
            "tools_upserted": self.tools_upserted,
            "apps_inserted": self.apps_inserted,
            "apps_updated": self.apps_updated,
            "tools_inserted": self.tools_inserted,
            "tools_updated": self.tools_updated,
            "apps_pruned": self.apps_pruned,
            "tools_pruned": self.tools_pruned,
            "skipped_batches": self.skipped_batches,
            "completed_batches": self.completed_batches,
            "matched_app_slugs": self.matched_app_slugs,
            "skipped_apps": self.skipped_apps,
            "diagnostics": self.diagnostics,
            "error": self.error,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_int_id(namespace: str, value: str) -> int:
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _normalize_app_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _stable_hash_for_rows(
    rows: Iterable[dict[str, Any]],
    *,
    fields: tuple[str, ...],
) -> str:
    normalized = []
    for row in rows:
        normalized.append({field: row.get(field) for field in fields})
    normalized.sort(key=_json_dumps)
    return hashlib.sha256(_json_dumps(normalized).encode()).hexdigest()


def app_catalog_row(
    app: dict[str, Any],
    *,
    default_backend_id: str | None = None,
) -> dict[str, Any]:
    slug = _normalize_app_slug(app.get("canonical_app_slug") or app.get("app_slug"))
    backend_id = str(app.get("backend_id") or default_backend_id or "provider")
    display_name = app.get("display_name") or app.get("app_display_name") or slug
    description = app.get("description") or ""
    source_type = app.get("source_type") or (
        "native" if backend_id == "unity_native" else "third_party"
    )
    auth_modes = app.get("auth_modes") or []
    available_scopes = (
        app.get("available_scopes") or app.get("available_scopes_json") or []
    )
    recommended_scopes = app.get("recommended_scopes") or []
    category = app.get("category")
    embedding_text = "\n".join(
        part
        for part in (
            f"Integration App: {display_name}",
            f"Slug: {slug}",
            f"Source: {source_type}",
            f"Category: {category}" if category else "",
            f"Description: {description}" if description else "",
            (
                f"Scopes: {', '.join(str(scope) for scope in available_scopes)}"
                if available_scopes
                else ""
            ),
        )
        if part
    )
    return {
        "app_id": _stable_int_id("integration_app", f"{backend_id}:{slug}"),
        "backend_id": backend_id,
        "provider_app_id": app.get("provider_app_id") or slug,
        "canonical_app_slug": slug,
        "display_name": display_name,
        "description": description,
        "category": category,
        "icon_url": app.get("icon_url") or app.get("app_icon_url"),
        "auth_modes": auth_modes,
        "available_scopes": available_scopes,
        "recommended_scopes": recommended_scopes,
        "tool_count": int(app.get("tool_count") or 0),
        "source_type": source_type,
        "source_label": app.get("source_label")
        or ("Native" if source_type == "native" else "Third-party"),
        "supported": bool(app.get("supported", True)),
        "raw_provider_metadata": app.get("raw_provider_metadata")
        or app.get("raw_provider_metadata_json")
        or {},
        "embedding_text": embedding_text,
    }


def _provider_integration_function_id(tool_id: str) -> int:
    digest = hashlib.sha256(
        f"IntegrationPrimitives.provider_backed:{tool_id}".encode(),
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _schema_properties(input_schema: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    properties = (
        input_schema.get("properties") if isinstance(input_schema, dict) else None
    )
    required = (
        set(input_schema.get("required") or [])
        if isinstance(input_schema, dict)
        else set()
    )
    if not isinstance(properties, dict) or not properties:
        return {}, set()
    return properties, required


def _schema_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "Any"
    for union_key in ("anyOf", "oneOf"):
        if union_key in schema and isinstance(schema[union_key], list):
            types = [
                _schema_type(item)
                for item in schema[union_key]
                if isinstance(item, dict) and item.get("type") != "null"
            ]
            return " | ".join(dict.fromkeys(types)) if types else "Any"
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        return (
            " | ".join(dict.fromkeys(_schema_type({"type": item}) for item in non_null))
            or "Any"
        )
    if raw_type == "array":
        item_type = _schema_type(schema.get("items"))
        return f"list[{item_type}]" if item_type != "Any" else "list"
    if raw_type == "object":
        return "dict"
    if isinstance(raw_type, str):
        return {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
        }.get(raw_type, "Any")
    return "Any"


def _schema_argspec(input_schema: dict[str, Any]) -> str:
    properties, required = _schema_properties(input_schema)
    if not properties:
        return "(**kwargs) -> dict"
    parts: list[str] = []
    for name, schema in properties.items():
        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or keyword.iskeyword(name)
        ):
            continue
        default = ""
        if isinstance(schema, dict) and "default" in schema and name not in required:
            default = f" = {schema['default']!r}"
        elif name not in required:
            default = " = None"
        parts.append(f"{name}: {_schema_type(schema)}{default}")
    return f"({', '.join(parts)}) -> dict" if parts else "(**kwargs) -> dict"


def _parameter_doc(input_schema: dict[str, Any]) -> str:
    properties, required = _schema_properties(input_schema)
    if not properties:
        return "Parameters\n----------\n**kwargs : Any\n    Provider arguments accepted by the integration tool."
    lines = ["Parameters", "----------"]
    for name, schema in properties.items():
        if not isinstance(name, str):
            continue
        required_label = "required" if name in required else "optional"
        default_text = ""
        description = ""
        if isinstance(schema, dict):
            if "default" in schema:
                default_text = f", default {schema['default']!r}"
            description = str(schema.get("description") or schema.get("title") or "")
        detail = f"{required_label}{default_text}."
        if description:
            detail = f"{detail} {description}"
        lines.extend([f"{name} : {_schema_type(schema)}", f"    {detail}"])
    return "\n".join(lines)


def _examples_doc(
    name: str,
    examples: list[Any],
    input_schema: dict[str, Any],
    description: str,
) -> str:
    example_payloads: list[dict[str, Any]] = []
    for example in examples:
        if isinstance(example, dict):
            args = (
                example.get("arguments")
                or example.get("input")
                or example.get("params")
                or example
            )
            if isinstance(args, dict):
                example_payloads.append(args)
        if len(example_payloads) >= 3:
            break
    if not example_payloads:
        properties, _required = _schema_properties(input_schema)
        synthetic: dict[str, Any] = {}
        for param, schema in properties.items():
            if not isinstance(param, str) or not isinstance(schema, dict):
                continue
            if "default" in schema:
                synthetic[param] = schema["default"]
            elif param in {"query", "q", "search_query"}:
                synthetic[param] = "is:unread"
            elif param in {"max_results", "limit", "page_size"}:
                synthetic[param] = 5
            elif schema.get("type") == "boolean":
                synthetic[param] = False
            if len(synthetic) >= 3:
                break
        if synthetic:
            example_payloads.append(synthetic)
    if not example_payloads:
        return "Examples\n--------\nNo provider examples are available. Inspect the Parameters section before calling."
    lines = ["Examples", "--------"]
    for payload in example_payloads:
        rendered = ", ".join(f"{key}={value!r}" for key, value in payload.items())
        lines.append(f"await {name}({rendered})")
    if "hydrate" in description.lower() or "message_id" in description.lower():
        lines.append(
            "For full message bodies, list message IDs first and hydrate individual messages when needed.",
        )
    return "\n".join(lines)


def _embedding_text(
    *,
    name: str,
    signature: str,
    app: str,
    tool: str,
    description: str,
    input_schema: dict[str, Any],
    examples: list[Any],
) -> str:
    properties, _required = _schema_properties(input_schema)
    parts = [
        f"Function Name: {name}",
        f"Signature: {signature}",
        f"App: {app}",
        f"Tool: {tool}",
        f"Purpose: {description}",
    ]
    parameter_names = ", ".join(str(key) for key in properties.keys())
    if parameter_names:
        parts.append(f"Parameters: {parameter_names}")
    example_terms = [
        json.dumps(example, sort_keys=True)
        for example in examples
        if isinstance(example, dict)
    ][:2]
    if example_terms:
        parts.append(f"Examples: {'; '.join(example_terms)}")
    return "\n".join(parts)


def tool_catalog_row(
    tool: dict[str, Any],
    *,
    default_backend_id: str | None = None,
) -> dict[str, Any]:
    item = dict(tool)
    backend = str(item.get("backend_id") or default_backend_id or "provider")
    app_slug = _normalize_app_slug(
        item.get("app_slug")
        or item.get("canonical_app_slug")
        or item.get("provider_app_id"),
    )
    tool_name = _normalize_app_slug(
        item.get("name") or item.get("canonical_name") or item.get("provider_tool_id"),
    )
    item["backend_id"] = backend
    item["app_slug"] = app_slug
    item.setdefault("tool_id", f"{backend}:{app_slug}:{tool_name}")
    item.setdefault("canonical_name", f"primitives.integrations.{app_slug}.{tool_name}")
    item.setdefault(
        "function_manager_name",
        f"primitives_integrations__{app_slug}__{tool_name}",
    )
    if "display_name" in item and "tool_display_name" not in item:
        item["tool_display_name"] = item["display_name"]

    tool_id = item["tool_id"]
    name = item["canonical_name"]
    app = item.get("app_display_name") or item.get("app_slug") or "integration"
    tool_label = item.get("tool_display_name") or name.rsplit(".", 1)[-1]
    provider_app_id = item.get("provider_app_id") or item.get("app_slug")
    provider_tool_id = item.get("provider_tool_id") or item.get("provider_action_id")
    app_icon_url = item.get("app_icon_url") or item.get("icon_url")
    required_scopes = item.get("required_scopes") or []
    action_class = item.get("action_class", "read")
    confirmation_required = bool(item.get("confirmation_required", False))
    behavior_hints = item.get("behavior_hints") or []
    input_schema = item.get("input_schema") or item.get("input_schema_json") or {}
    output_schema = item.get("output_schema") or item.get("output_schema_json") or {}
    examples = item.get("examples") or item.get("examples_json") or []
    signature = _schema_argspec(input_schema)
    parameter_doc = _parameter_doc(input_schema)
    examples_doc = _examples_doc(
        name,
        examples,
        input_schema,
        str(item.get("description") or ""),
    )
    docstring = (
        f"{tool_label}\n\n"
        f"Use this {app} integration primitive when you need to {item.get('description', 'run this provider action')}.\n\n"
        f"Call signature\n--------------\n{name}{signature}\n\n"
        f"{parameter_doc}\n\n"
        "Returns\n-------\n"
        "dict\n"
        "    Provider execution envelope returned by Orchestra. Treat non-ok "
        "statuses such as confirmation_required, missing_scope, expired, "
        "blocked_by_policy, or error as actionable outcomes to explain to "
        "the user.\n\n"
        f"{examples_doc}\n\n"
        "Safety\n------\n"
        f"Action class: {action_class}. "
        f"Confirmation required: {confirmation_required}. "
        "Use the approved confirmation flow for sensitive, write, destructive, "
        "or bulk-export actions."
    )
    metadata = {
        "source": "provider_backed",
        "integration": {
            "tool_id": tool_id,
            "backend_id": backend,
            "app_slug": item.get("app_slug"),
            "input_schema": input_schema,
            "output_schema": output_schema,
            "examples": examples,
            "source_type": "third_party",
            "namespace": "primitives.integrations",
            "provider_app_id": provider_app_id,
            "provider_tool_id": provider_tool_id,
            "labels": {
                "app_display_name": app,
                "app_icon_url": app_icon_url,
                "tool_display_name": tool_label,
            },
            "app_display_name": app,
            "app_icon_url": app_icon_url,
            "tool_display_name": tool_label,
            "required_scopes": required_scopes,
            "action_class": action_class,
            "behavior_hints": behavior_hints,
            "confirmation_required": confirmation_required,
            "schema_available": item.get("schema_available", True),
        },
    }
    return {
        "function_id": _provider_integration_function_id(str(tool_id)),
        "language": "python",
        "name": name,
        "argspec": signature,
        "docstring": docstring,
        "implementation": None,
        "depends_on": [],
        "precondition": None,
        "embedding_text": _embedding_text(
            name=name,
            signature=signature,
            app=str(app),
            tool=str(tool_label),
            description=str(item.get("description") or ""),
            input_schema=input_schema,
            examples=examples,
        ),
        "guidance_ids": item.get("guidance_ids") or [],
        "verify": confirmation_required
        or action_class in {"write", "destructive", "bulk_export"},
        "is_primitive": True,
        "primitive_class": "unity.integrations.primitives.IntegrationPrimitives",
        "primitive_method": item.get("function_manager_name")
        or name.replace(".", "__"),
        "metadata": metadata,
    }


def fetch_provider_catalog(
    session: Session,
    body: IntegrationCatalogSyncRequest,
) -> ProviderCatalogFetchResult:
    """Fetch and normalize provider catalog rows without writing legacy tables."""

    if body.backend_id != "composio":
        raise NotImplementedError(
            f"Builtins direct sync currently supports composio, got {body.backend_id}",
        )

    from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
    from orchestra.web.api.integrations.operations import (
        _action_class_from_behavior_hints,
        _composio_auth_modes,
        _composio_behavior_hints,
        _composio_canonical_app_slug,
        _composio_icon_url,
        _composio_tool_input_schema,
        _composio_tool_name,
        _composio_tool_output_schema,
        _composio_tool_scopes,
        _composio_toolkit_slug,
        get_provider_adapter,
        seed_default_provider_catalog,
    )

    seed_default_provider_catalog(session)
    backend = IntegrationProviderDAO(session).get_backend("composio")
    config = (backend.config_json if backend else {}) or {}
    adapter = get_provider_adapter(
        "composio",
        backend_config=config,
        backend_status=backend.status if backend else "enabled",
        require_live=True,
    )
    requested_slugs = [slug.strip().upper() for slug in body.app_slugs if slug.strip()]
    requested_set = set(requested_slugs)
    toolkits = adapter.list_toolkits()
    toolkits_by_slug = {
        str(
            toolkit.get("slug")
            or toolkit.get("toolkit_slug")
            or toolkit.get("id")
            or "",
        ).upper(): toolkit
        for toolkit in toolkits
        if toolkit.get("slug") or toolkit.get("toolkit_slug") or toolkit.get("id")
    }
    selected_toolkit_slugs = (
        sorted(toolkits_by_slug)
        if body.include_all_managed_apps or not requested_slugs
        else [slug for slug in requested_slugs if slug in toolkits_by_slug]
    )
    skipped_apps = [
        {"slug": slug, "reason": "not_found"}
        for slug in requested_slugs
        if slug not in toolkits_by_slug
    ]
    if requested_slugs and not selected_toolkit_slugs:
        return ProviderCatalogFetchResult(
            apps=[],
            tools=[],
            skipped_apps=skipped_apps,
            requested_app_slugs=requested_slugs,
            matched_app_slugs=[],
            sync_mode=body.sync_mode or "partial",
            cache_version=body.cache_version,
            warning="No requested Composio apps matched the live provider catalog.",
        )

    apps: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    auth_configs_created = 0
    auth_configs_reused = 0
    should_create_auth_configs = body.create_auth_configs and bool(requested_slugs)
    syncable_toolkit_slugs: list[str] = []
    for toolkit_slug in selected_toolkit_slugs:
        toolkit = toolkits_by_slug[toolkit_slug]
        canonical_app_slug = _composio_canonical_app_slug(toolkit_slug)
        auth_config_id = None
        if should_create_auth_configs and "oauth" in _composio_auth_modes(toolkit):
            try:
                auth_config_id = adapter.get_or_create_auth_config(toolkit_slug)
            except Exception as exc:
                skipped_apps.append(
                    {
                        "slug": toolkit_slug,
                        "reason": "auth_config_failed",
                        "message": str(exc)[:300],
                    },
                )
                continue
            if getattr(adapter, "last_auth_config_was_created", False):
                auth_configs_created += 1
            elif auth_config_id:
                auth_configs_reused += 1
        raw_provider_metadata = {
            "source": "composio_live_sync",
            "toolkit_slug": toolkit_slug,
            "toolkit_version": toolkit.get("version"),
            "managed_auth": True,
            "raw_toolkit": toolkit,
        }
        if auth_config_id:
            raw_provider_metadata["auth_config_id"] = auth_config_id
        apps.append(
            {
                "backend_id": "composio",
                "provider_app_id": toolkit_slug,
                "canonical_app_slug": canonical_app_slug,
                "display_name": toolkit.get("name")
                or canonical_app_slug.replace("_", " ").title(),
                "description": toolkit.get("description"),
                "category": toolkit.get("category"),
                "icon_url": _composio_icon_url(toolkit),
                "auth_modes": _composio_auth_modes(toolkit),
                "available_scopes": [],
                "raw_provider_metadata": raw_provider_metadata,
            },
        )
        syncable_toolkit_slugs.append(toolkit_slug)

    if body.sync_tools and syncable_toolkit_slugs:
        tool_limit = body.tool_limit_per_app if body.tool_limit_per_app > 0 else None
        tool_fetch_concurrency = max(1, int(config.get("tool_fetch_concurrency", 8)))

        def fetch_tools(toolkit_slug: str) -> tuple[str, list[dict[str, Any]]]:
            return (
                toolkit_slug,
                adapter.list_tools(toolkit_slug=toolkit_slug, limit=tool_limit),
            )

        raw_tools_by_toolkit: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=tool_fetch_concurrency) as executor:
            future_by_slug = {
                executor.submit(fetch_tools, toolkit_slug): toolkit_slug
                for toolkit_slug in syncable_toolkit_slugs
            }
            for future in as_completed(future_by_slug):
                toolkit_slug, toolkit_tools = future.result()
                raw_tools_by_toolkit[toolkit_slug] = toolkit_tools

        for toolkit_slug in syncable_toolkit_slugs:
            toolkit = toolkits_by_slug[toolkit_slug]
            canonical_app_slug = _composio_canonical_app_slug(toolkit_slug)
            for tool in raw_tools_by_toolkit.get(toolkit_slug, []):
                provider_tool_id = str(tool.get("slug") or tool.get("id") or "")
                if not provider_tool_id:
                    continue
                tool_app_slug = (_composio_toolkit_slug(tool) or toolkit_slug).upper()
                if (
                    not body.include_all_managed_apps
                    and requested_set
                    and tool_app_slug not in requested_set
                    and tool_app_slug != toolkit_slug
                ):
                    continue
                tool_name = _composio_tool_name(provider_tool_id, toolkit_slug)
                behavior_hints = _composio_behavior_hints(tool)
                action_class = _action_class_from_behavior_hints(behavior_hints)
                tools.append(
                    {
                        "backend_id": "composio",
                        "provider_app_id": toolkit_slug,
                        "canonical_app_slug": canonical_app_slug,
                        "provider_tool_id": provider_tool_id,
                        "name": tool_name,
                        "display_name": tool.get("name")
                        or tool_name.replace("_", " ").title(),
                        "description": tool.get("description")
                        or tool_name.replace("_", " ").title(),
                        "required_scopes": _composio_tool_scopes(tool),
                        "input_schema": _composio_tool_input_schema(tool),
                        "output_schema": _composio_tool_output_schema(tool),
                        "action_class": action_class,
                        "behavior_hints": behavior_hints,
                        "confirmation_required": action_class
                        in {"write", "destructive", "bulk_export"},
                        "category": toolkit.get("category"),
                        "tags": [
                            canonical_app_slug,
                            str(toolkit.get("category") or "").lower(),
                        ],
                        "raw_provider_metadata": {
                            "source": "composio_live_sync",
                            "toolkit_slug": toolkit_slug,
                            "tool_version": tool.get("version"),
                            "raw_tool": tool,
                        },
                    },
                )

    return ProviderCatalogFetchResult(
        apps=apps,
        tools=tools,
        skipped_apps=skipped_apps,
        requested_app_slugs=requested_slugs,
        matched_app_slugs=[
            str(app["canonical_app_slug"])
            for app in apps
            if app.get("canonical_app_slug")
        ],
        sync_mode=body.sync_mode
        or ("full" if body.include_all_managed_apps else "partial"),
        cache_version=body.cache_version,
        auth_configs_created=auth_configs_created,
        auth_configs_reused=auth_configs_reused,
    )


def _fetch_provider_catalog_with_retry(
    session: Session,
    body: IntegrationCatalogSyncRequest,
    *,
    attempts: int | None = None,
    initial_delay_seconds: float | None = None,
) -> ProviderCatalogFetchResult:
    max_attempts = max(
        1,
        int(
            (
                attempts
                if attempts is not None
                else os.getenv("ORCHESTRA_BUILTINS_SYNC_PROVIDER_FETCH_ATTEMPTS", "3")
            ),
        ),
    )
    delay_seconds = max(
        0.0,
        float(
            (
                initial_delay_seconds
                if initial_delay_seconds is not None
                else os.getenv(
                    "ORCHESTRA_BUILTINS_SYNC_PROVIDER_FETCH_RETRY_SECONDS",
                    "1",
                )
            ),
        ),
    )
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fetch_provider_catalog(session, body)
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            logger.warning(
                "Builtins provider fetch failed; retrying attempt=%s/%s backend=%s "
                "sync_tools=%s app_slugs=%s error=%s",
                attempt,
                max_attempts,
                body.backend_id,
                body.sync_tools,
                body.app_slugs,
                str(exc)[:300],
            )
            time.sleep(delay_seconds * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


def _context(session: Session, *, project_id: int, name: str) -> Context:
    context = (
        session.query(Context)
        .filter(Context.project_id == project_id, Context.name == name)
        .one_or_none()
    )
    if not context:
        raise RuntimeError(f"Builtins context missing after ensure: {name}")
    return context


def ensure_builtins_catalog_contexts(session: Session) -> dict[str, Context]:
    project = (
        session.query(Project)
        .filter(Project.name == BUILTINS_PROJECT_NAME)
        .order_by(Project.is_public_read.desc(), Project.id.asc())
        .first()
    )
    if project is None:
        raise RuntimeError(
            "Builtins project does not exist; create it with the shared owner first",
        )

    context_dao = ContextDAO(session)
    context_dao.create(
        project_id=project.id,
        name=BUILTINS_INTEGRATION_APPS_CONTEXT,
        description="Public integration app catalogue.",
        unique_keys={"app_id": "int"},
    )
    context_dao.create(
        project_id=project.id,
        name=BUILTINS_INTEGRATION_TOOLS_CONTEXT,
        description="Public integration tool catalogue.",
        unique_keys={"function_id": "int"},
    )
    context_dao.create(
        project_id=project.id,
        name=BUILTINS_INTEGRATION_META_CONTEXT,
        description="Seeding state for the integration catalogue.",
        unique_keys={"meta_id": "int"},
    )
    return {
        "project": project,
        "apps": _context(
            session,
            project_id=project.id,
            name=BUILTINS_INTEGRATION_APPS_CONTEXT,
        ),
        "tools": _context(
            session,
            project_id=project.id,
            name=BUILTINS_INTEGRATION_TOOLS_CONTEXT,
        ),
        "meta": _context(
            session,
            project_id=project.id,
            name=BUILTINS_INTEGRATION_META_CONTEXT,
        ),
    }


def _ensure_field_types(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    rows: Iterable[dict[str, Any]],
) -> None:
    field_type_dao = FieldTypeDAO(session)
    seen: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if key in seen:
                continue
            seen.add(key)
            field_type_dao.create_field_type_if_absent(
                project_id=project_id,
                field_name=key,
                value=value,
                context_id=context_id,
                field_category="entry",
                infer_type=True,
            )


def _lock_unique_key(
    session: Session,
    *,
    context_id: int,
    key_hash: str,
) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"builtins-catalog:{context_id}:{key_hash}"},
    )


def _find_existing_log_id(
    session: Session,
    *,
    context_id: int,
    key_columns: list[str],
    key_values: dict[str, Any],
    key_hash: str,
) -> int | None:
    lookup = session.execute(
        text(
            """
            SELECT luc.log_event_id
            FROM log_unique_constraint luc
            JOIN log_event_context lec ON lec.log_event_id = luc.log_event_id
            WHERE luc.context_id = :context_id
              AND lec.context_id = :context_id
              AND luc.field_name = :field_name
              AND luc.value_hash = :value_hash
            LIMIT 1
            """,
        ),
        {
            "context_id": context_id,
            "field_name": COMPOSITE_KEY_FIELD,
            "value_hash": key_hash,
        },
    ).scalar_one_or_none()
    if lookup is not None:
        return int(lookup)

    params: dict[str, Any] = {"context_id": context_id}
    conditions = []
    for index, column in enumerate(key_columns):
        param = f"value_{index}"
        conditions.append(f"le.data ->> :column_{index} = :{param}")
        params[f"column_{index}"] = column
        params[param] = str(key_values[column])
    sql = f"""
        SELECT le.id
        FROM log_event le
        JOIN log_event_context lec ON lec.log_event_id = le.id
        WHERE lec.context_id = :context_id
          AND {' AND '.join(conditions)}
        LIMIT 1
    """
    found = session.execute(text(sql), params).scalar_one_or_none()
    return int(found) if found is not None else None


def _upsert_constraint(
    session: Session,
    *,
    context_id: int,
    log_event_id: int,
    value_hash: str,
) -> None:
    stmt = pg_insert(LogUniqueConstraint).values(
        context_id=context_id,
        field_name=COMPOSITE_KEY_FIELD,
        value_hash=value_hash,
        log_event_id=log_event_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["context_id", "field_name", "value_hash"],
        set_={"log_event_id": log_event_id},
    )
    session.execute(stmt)


def _insert_new_constraint(
    session: Session,
    *,
    context_id: int,
    log_event_id: int,
    value_hash: str,
) -> bool:
    stmt = pg_insert(LogUniqueConstraint).values(
        context_id=context_id,
        field_name=COMPOSITE_KEY_FIELD,
        value_hash=value_hash,
        log_event_id=log_event_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["context_id", "field_name", "value_hash"],
    ).returning(LogUniqueConstraint.log_event_id)
    return session.execute(stmt).scalar_one_or_none() is not None


def _lookup_constraint_log_id(
    session: Session,
    *,
    context_id: int,
    value_hash: str,
) -> int | None:
    value = session.execute(
        text(
            """
            SELECT log_event_id
            FROM log_unique_constraint
            WHERE context_id = :context_id
              AND field_name = :field_name
              AND value_hash = :value_hash
            """,
        ),
        {
            "context_id": context_id,
            "field_name": COMPOSITE_KEY_FIELD,
            "value_hash": value_hash,
        },
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def upsert_context_rows(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    key_columns: list[str],
    rows: Iterable[dict[str, Any]],
) -> dict[str, int]:
    """Upsert Builtins rows by context unique key, preserving log_event IDs."""

    rows_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(column) for column in key_columns)
        if any(value is None for value in key):
            raise ValueError(
                f"Missing Builtins unique key values for {key_columns}: {row}",
            )
        rows_by_key[key] = dict(row)

    if not rows_by_key:
        return {"inserted": 0, "updated": 0, "total": 0}

    _ensure_field_types(
        session,
        project_id=project_id,
        context_id=context_id,
        rows=rows_by_key.values(),
    )

    inserted = 0
    updated = 0
    now = datetime.now(timezone.utc)
    from orchestra.db.scope import owner_key_for_context

    owner_key_value = owner_key_for_context(session, context_id)
    id_to_text: dict[int, str] = {}
    for row in rows_by_key.values():
        key_values = {column: row[column] for column in key_columns}
        key_hash = UniqueConstraintDAO.hash_composite(key_values, key_columns)
        _lock_unique_key(session, context_id=context_id, key_hash=key_hash)
        log_event_id = _find_existing_log_id(
            session,
            context_id=context_id,
            key_columns=key_columns,
            key_values=key_values,
            key_hash=key_hash,
        )
        data_json = json.dumps(row, default=str)
        if log_event_id is None:
            new_log_event_id = session.execute(
                text(
                    """
                    INSERT INTO log_event (project_id, data, created_at, updated_at, owner_key)
                    VALUES (:project_id, CAST(:data AS jsonb), :created_at, :updated_at, :owner_key)
                    RETURNING id
                    """,
                ),
                {
                    "project_id": project_id,
                    "data": data_json,
                    "created_at": now,
                    "updated_at": now,
                    "owner_key": owner_key_value,
                },
            ).scalar_one()
            association = pg_insert(LogEventContext).values(
                project_id=project_id,
                log_event_id=new_log_event_id,
                context_id=context_id,
                owner_key=owner_key_value,
            )
            association = association.on_conflict_do_nothing(
                index_elements=[
                    "project_id",
                    "owner_key",
                    "log_event_id",
                    "context_id",
                ],
            )
            session.execute(association)
            inserted_constraint = _insert_new_constraint(
                session,
                context_id=context_id,
                log_event_id=int(new_log_event_id),
                value_hash=key_hash,
            )
            if inserted_constraint:
                log_event_id = new_log_event_id
                inserted += 1
            else:
                winner_id = _lookup_constraint_log_id(
                    session,
                    context_id=context_id,
                    value_hash=key_hash,
                )
                if winner_id is None:
                    raise RuntimeError("Unique-key race lost but no winner was found")
                session.execute(
                    text("DELETE FROM log_event WHERE id = :log_event_id"),
                    {"log_event_id": new_log_event_id},
                )
                log_event_id = winner_id
                session.execute(
                    text(
                        """
                        UPDATE log_event
                        SET data = CAST(:data AS jsonb),
                            updated_at = :updated_at
                        WHERE id = :log_event_id
                        """,
                    ),
                    {
                        "data": data_json,
                        "updated_at": now,
                        "log_event_id": log_event_id,
                    },
                )
                updated += 1
        else:
            session.execute(
                text(
                    """
                    UPDATE log_event
                    SET data = CAST(:data AS jsonb),
                        updated_at = :updated_at
                    WHERE id = :log_event_id
                    """,
                ),
                {
                    "data": data_json,
                    "updated_at": now,
                    "log_event_id": log_event_id,
                },
            )
            updated += 1
            association = pg_insert(LogEventContext).values(
                project_id=project_id,
                log_event_id=log_event_id,
                context_id=context_id,
                owner_key=owner_key_value,
            )
            association = association.on_conflict_do_nothing(
                index_elements=[
                    "project_id",
                    "owner_key",
                    "log_event_id",
                    "context_id",
                ],
            )
            session.execute(association)
            _upsert_constraint(
                session,
                context_id=context_id,
                log_event_id=int(log_event_id),
                value_hash=key_hash,
            )
        embedding_text = row.get("embedding_text")
        if isinstance(embedding_text, str) and embedding_text.strip():
            id_to_text[int(log_event_id)] = embedding_text

    if id_to_text:
        from orchestra.web.api.log.python2SQL.helpers import (
            _queue_embeddings_for_generation,
        )

        _queue_embeddings_for_generation(
            session,
            id_to_text,
            model=None,
            dimensions=None,
            key="_embedding_text_emb",
        )
    else:
        session.flush()
    return {"inserted": inserted, "updated": updated, "total": len(rows_by_key)}


def _delete_stale_context_rows(
    session: Session,
    *,
    context_id: int,
    backend_id: str,
    key_column: str,
    keep_values: Iterable[Any],
    app_slugs: Iterable[str] | None = None,
) -> int:
    if key_column not in {"app_id", "function_id"}:
        raise ValueError(f"Unsupported Builtins prune key column: {key_column}")
    keep_value_strings = [str(value) for value in keep_values]
    normalized_app_slugs = [
        _normalize_app_slug(slug) for slug in (app_slugs or []) if str(slug).strip()
    ]
    app_slug_filter = ""
    params: dict[str, Any] = {
        "context_id": context_id,
        "backend_id": backend_id,
        "keep_values": keep_value_strings,
    }
    if normalized_app_slugs:
        app_slug_filter = (
            "AND (le.data #>> '{metadata,integration,app_slug}') = "
            "ANY(CAST(:app_slugs AS text[]))"
        )
        params["app_slugs"] = normalized_app_slugs

    deleted_ids = session.execute(
        text(
            f"""
            WITH stale AS (
                SELECT le.id
                FROM log_event le
                JOIN log_event_context lec ON lec.log_event_id = le.id
                WHERE lec.context_id = :context_id
                  AND le.data ->> 'backend_id' = :backend_id
                  AND (le.data ->> '{key_column}') <> ALL(CAST(:keep_values AS text[]))
                  {app_slug_filter}
            ),
            deleted_unique AS (
                DELETE FROM log_unique_constraint luc
                USING stale
                WHERE luc.context_id = :context_id
                  AND luc.log_event_id = stale.id
                RETURNING luc.log_event_id
            ),
            deleted_context AS (
                DELETE FROM log_event_context lec
                USING stale
                WHERE lec.context_id = :context_id
                  AND lec.log_event_id = stale.id
                RETURNING lec.log_event_id
            ),
            deleted_orphans AS (
                DELETE FROM log_event le
                USING stale
                WHERE le.id = stale.id
                  AND NOT EXISTS (
                      SELECT 1
                      FROM log_event_context remaining
                      WHERE remaining.log_event_id = le.id
                  )
                RETURNING le.id
            )
            SELECT COUNT(*) FROM stale
            """,
        ),
        params,
    ).scalar_one()
    return int(deleted_ids or 0)


def prune_stale_app_rows(
    session: Session,
    *,
    context_id: int,
    backend_id: str,
    keep_app_ids: Iterable[Any],
) -> int:
    return _delete_stale_context_rows(
        session,
        context_id=context_id,
        backend_id=backend_id,
        key_column="app_id",
        keep_values=keep_app_ids,
    )


def prune_stale_tool_rows(
    session: Session,
    *,
    context_id: int,
    backend_id: str,
    app_slugs: Iterable[str],
    keep_function_ids: Iterable[Any],
) -> int:
    return _delete_stale_context_rows(
        session,
        context_id=context_id,
        backend_id=backend_id,
        key_column="function_id",
        keep_values=keep_function_ids,
        app_slugs=app_slugs,
    )


def _meta_id(kind: str, *parts: Any) -> int:
    return _stable_int_id("integration_meta", f"{kind}:{_json_dumps(parts)}")


def _meta_row(
    *,
    kind: str,
    request: BuiltinsSyncRequest,
    **values: Any,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "kind": kind,
        "backend_id": request.backend_id,
        "environment": request.environment,
        "desired_hash": request.desired_hash,
        "cache_version": request.cache_version,
        "run_id": request.run_id,
        "updated_at": now,
        **values,
    }
    row["meta_id"] = _meta_id(
        kind,
        request.backend_id,
        request.environment,
        request.desired_hash,
        request.cache_version,
        values.get("unit_key") or values.get("checkpoint_key") or values.get("job_key"),
    )
    return row


def write_bootstrap_state(
    session: Session,
    *,
    request: BuiltinsSyncRequest,
    status: str,
    result: BuiltinsSyncResult | None = None,
    error: str | None = None,
) -> None:
    """Persist deployment-visible bootstrap state for worker/job executions."""

    payload = result.to_payload() if result else {}
    diagnostics = {
        "seed_owner": "public-builtins",
        "builtins_seeded": status == "success",
        "executor": "orchestra_worker",
        "run_id": request.run_id,
        "request_uri": request.request_uri,
        "cache_version": request.cache_version,
        "sync_mode": request.sync_payload.get("sync_mode"),
        "requested_app_slugs": list(request.sync_payload.get("app_slugs") or []),
        "prune_unlisted_apps": request.prune_unlisted_apps,
        **(payload.get("diagnostics") or {}),
        "status": status,
        "error": error,
        "apps_upserted": int(payload.get("apps_upserted", 0) or 0),
        "tools_upserted": int(payload.get("tools_upserted", 0) or 0),
        "skipped_batches": int(payload.get("skipped_batches", 0) or 0),
        "completed_batches": int(payload.get("completed_batches", 0) or 0),
        "elapsed_seconds": payload.get("elapsed_seconds"),
    }
    values = {
        "desired_hash": request.desired_hash,
        "desired_config_json": request.desired_config
        or {
            "backend_id": request.backend_id,
            "environment": request.environment,
            "sync": request.sync_payload,
            "cache_version": request.cache_version,
        },
        "last_status": status,
        "last_error": error,
        "apps_upserted": int(payload.get("apps_upserted", 0) or 0),
        "tools_upserted": int(payload.get("tools_upserted", 0) or 0),
        "last_sync_diagnostics_json": diagnostics,
    }
    if status == "success":
        values["last_synced_at"] = datetime.now(timezone.utc)
    IntegrationProviderDAO(session).upsert_bootstrap_state(
        environment=request.environment,
        backend_id=request.backend_id,
        values=values,
    )
    session.flush()


def _get_meta_row_by_id(
    session: Session,
    *,
    context_id: int,
    meta_id: int,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT le.data
            FROM log_event le
            JOIN log_event_context lec ON lec.log_event_id = le.id
            WHERE lec.context_id = :context_id
              AND le.data ->> 'meta_id' = :meta_id
            LIMIT 1
            """,
        ),
        {"context_id": context_id, "meta_id": str(meta_id)},
    ).scalar_one_or_none()
    return dict(row) if isinstance(row, dict) else None


def _checkpoint_key(
    request: BuiltinsSyncRequest,
    *,
    batch_index: int,
    app_slugs: list[str],
) -> str:
    return request.checkpoint_key or _json_dumps(
        {
            "backend_id": request.backend_id,
            "desired_hash": request.desired_hash,
            "cache_version": request.cache_version,
            "batch_index": batch_index,
            "app_slugs": sorted(_normalize_app_slug(slug) for slug in app_slugs),
        },
    )


def _checkpoint_complete(
    session: Session,
    *,
    meta_context_id: int,
    request: BuiltinsSyncRequest,
    batch_index: int,
    app_slugs: list[str],
) -> bool:
    key = _checkpoint_key(request, batch_index=batch_index, app_slugs=app_slugs)
    row = _meta_row(
        kind="batch_checkpoint",
        request=request,
        checkpoint_key=key,
    )
    existing = _get_meta_row_by_id(
        session,
        context_id=meta_context_id,
        meta_id=int(row["meta_id"]),
    )
    return bool(existing and existing.get("status") == "success")


def _write_meta_rows(
    session: Session,
    *,
    project_id: int,
    meta_context_id: int,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    return upsert_context_rows(
        session,
        project_id=project_id,
        context_id=meta_context_id,
        key_columns=["meta_id"],
        rows=rows,
    )


def _write_unit_hash(
    session: Session,
    *,
    project_id: int,
    meta_context_id: int,
    request: BuiltinsSyncRequest,
    unit_key: str,
    unit_hash: str,
    app_slug: str,
) -> None:
    _write_meta_rows(
        session,
        project_id=project_id,
        meta_context_id=meta_context_id,
        rows=[
            _meta_row(
                kind="unit_hash",
                request=request,
                unit_key=unit_key,
                app_slug=app_slug,
                unit_hash=unit_hash,
                status="success",
            ),
        ],
    )


def _unit_hash_matches(
    session: Session,
    *,
    meta_context_id: int,
    request: BuiltinsSyncRequest,
    unit_key: str,
    unit_hash: str,
) -> bool:
    row = _meta_row(
        kind="unit_hash",
        request=request,
        unit_key=unit_key,
    )
    existing = _get_meta_row_by_id(
        session,
        context_id=meta_context_id,
        meta_id=int(row["meta_id"]),
    )
    return bool(
        existing
        and existing.get("status") == "success"
        and existing.get("unit_hash") == unit_hash,
    )


def _build_sync_payload(
    request: BuiltinsSyncRequest,
    *,
    sync_tools: bool,
    app_slugs: list[str] | None = None,
    full: bool = False,
) -> IntegrationCatalogSyncRequest:
    payload = {
        **request.sync_payload,
        "backend_id": request.backend_id,
        "cache_version": request.cache_version,
        "sync_tools": sync_tools,
        "app_slugs": app_slugs if app_slugs is not None else request.app_slugs,
    }
    if full:
        payload["sync_mode"] = "full"
        payload["include_all_managed_apps"] = True
        payload["app_slugs"] = []
    elif app_slugs is not None:
        payload["sync_mode"] = "partial"
        payload["include_all_managed_apps"] = False
    return IntegrationCatalogSyncRequest(**payload)


def _materialize_apps(
    session: Session,
    *,
    project_id: int,
    apps_context_id: int,
    meta_context_id: int,
    request: BuiltinsSyncRequest,
    apps: list[dict[str, Any]],
    prune_stale: bool = False,
) -> dict[str, int]:
    rows_by_slug = {
        str(row["canonical_app_slug"]): row
        for row in (
            app_catalog_row(app, default_backend_id=request.backend_id) for app in apps
        )
    }
    changed_rows: list[dict[str, Any]] = []
    for slug, row in rows_by_slug.items():
        unit_key = f"app:{request.backend_id}:{slug}"
        unit_hash = _stable_hash_for_rows([row], fields=tuple(sorted(row.keys())))
        if _unit_hash_matches(
            session,
            meta_context_id=meta_context_id,
            request=request,
            unit_key=unit_key,
            unit_hash=unit_hash,
        ):
            continue
        changed_rows.append(row)
        _write_unit_hash(
            session,
            project_id=project_id,
            meta_context_id=meta_context_id,
            request=request,
            unit_key=unit_key,
            unit_hash=unit_hash,
            app_slug=slug,
        )
    counts = upsert_context_rows(
        session,
        project_id=project_id,
        context_id=apps_context_id,
        key_columns=["app_id"],
        rows=changed_rows,
    )
    counts["pruned"] = (
        prune_stale_app_rows(
            session,
            context_id=apps_context_id,
            backend_id=request.backend_id,
            keep_app_ids=[row["app_id"] for row in rows_by_slug.values()],
        )
        if prune_stale
        else 0
    )
    return counts


def _materialize_tools(
    session: Session,
    *,
    project_id: int,
    tools_context_id: int,
    meta_context_id: int,
    request: BuiltinsSyncRequest,
    tools: list[dict[str, Any]],
    prune_stale: bool = False,
    prune_app_slugs: list[str] | None = None,
) -> dict[str, int]:
    rows_by_slug: dict[str, list[dict[str, Any]]] = {}
    for row in (
        tool_catalog_row(tool, default_backend_id=request.backend_id) for tool in tools
    ):
        integration = (row.get("metadata") or {}).get("integration") or {}
        app_slug = str(integration.get("app_slug") or "")
        rows_by_slug.setdefault(app_slug, []).append(row)

    changed_rows: list[dict[str, Any]] = []
    current_function_ids: list[int] = []
    for slug, rows in rows_by_slug.items():
        deduped = {int(row["function_id"]): row for row in rows}
        current_function_ids.extend(deduped)
        unit_key = f"tools:{request.backend_id}:{slug}"
        unit_hash = _stable_hash_for_rows(
            deduped.values(),
            fields=(
                "function_id",
                "name",
                "argspec",
                "docstring",
                "metadata",
                "embedding_text",
            ),
        )
        if _unit_hash_matches(
            session,
            meta_context_id=meta_context_id,
            request=request,
            unit_key=unit_key,
            unit_hash=unit_hash,
        ):
            continue
        changed_rows.extend(deduped.values())
        _write_unit_hash(
            session,
            project_id=project_id,
            meta_context_id=meta_context_id,
            request=request,
            unit_key=unit_key,
            unit_hash=unit_hash,
            app_slug=slug,
        )
    counts = upsert_context_rows(
        session,
        project_id=project_id,
        context_id=tools_context_id,
        key_columns=["function_id"],
        rows=changed_rows,
    )
    counts["pruned"] = (
        prune_stale_tool_rows(
            session,
            context_id=tools_context_id,
            backend_id=request.backend_id,
            app_slugs=prune_app_slugs or sorted(rows_by_slug),
            keep_function_ids=current_function_ids,
        )
        if prune_stale
        else 0
    )
    return counts


def run_builtins_sync(
    session_factory: Callable[[], Session],
    request: BuiltinsSyncRequest,
) -> BuiltinsSyncResult:
    started_at = time.perf_counter()
    result = BuiltinsSyncResult(status="running")
    result.run_id = request.run_id
    result.desired_hash = request.desired_hash
    result.request_uri = request.request_uri
    with session_factory() as session:
        contexts = ensure_builtins_catalog_contexts(session)
        project = contexts["project"]
        meta_context = contexts["meta"]
        _write_meta_rows(
            session,
            project_id=project.id,
            meta_context_id=meta_context.id,
            rows=[
                _meta_row(
                    kind="job_state",
                    request=request,
                    job_key="current",
                    status="running",
                    mode=request.mode,
                ),
            ],
        )
        session.commit()

    app_started_at = time.perf_counter()
    app_body = _build_sync_payload(
        request,
        sync_tools=False,
        full=not request.app_slugs,
    )
    with session_factory() as session:
        app_fetch = _fetch_provider_catalog_with_retry(session, app_body)
        contexts = ensure_builtins_catalog_contexts(session)
        project = contexts["project"]
        app_counts = _materialize_apps(
            session,
            project_id=project.id,
            apps_context_id=contexts["apps"].id,
            meta_context_id=contexts["meta"].id,
            request=request,
            apps=app_fetch.apps,
            prune_stale=request.prune_unlisted_apps,
        )
        session.commit()
        result.apps_inserted = app_counts["inserted"]
        result.apps_updated = app_counts["updated"]
        result.apps_upserted = app_counts["total"]
        result.apps_pruned = app_counts.get("pruned", 0)
        result.matched_app_slugs = app_fetch.matched_app_slugs
        result.skipped_apps = app_fetch.skipped_apps
        result.diagnostics.update(
            {
                "sync_mode": app_fetch.sync_mode,
                "requested_app_slugs": app_fetch.requested_app_slugs,
                "cache_version": app_fetch.cache_version,
                "auth_configs_created": app_fetch.auth_configs_created,
                "auth_configs_reused": app_fetch.auth_configs_reused,
                "app_phase_seconds": round(time.perf_counter() - app_started_at, 3),
            },
        )

    if request.mode == "apps" or not request.sync_payload.get("sync_tools", True):
        result.status = "success"
        result.elapsed_seconds = round(time.perf_counter() - started_at, 3)
        with session_factory() as session:
            contexts = ensure_builtins_catalog_contexts(session)
            project = contexts["project"]
            _write_meta_rows(
                session,
                project_id=project.id,
                meta_context_id=contexts["meta"].id,
                rows=[
                    _meta_row(
                        kind="job_state",
                        request=request,
                        job_key="current",
                        status="success",
                        apps_upserted=result.apps_upserted,
                        tools_upserted=result.tools_upserted,
                        apps_pruned=result.apps_pruned,
                        tools_pruned=result.tools_pruned,
                        skipped_batches=result.skipped_batches,
                        completed_batches=result.completed_batches,
                    ),
                ],
            )
            session.commit()
        return result

    matched_slugs = result.matched_app_slugs or request.app_slugs
    batches = [
        matched_slugs[index : index + request.batch_size]
        for index in range(0, len(matched_slugs), request.batch_size)
    ]
    if not batches:
        result.status = "success"
        result.elapsed_seconds = round(time.perf_counter() - started_at, 3)
        with session_factory() as session:
            contexts = ensure_builtins_catalog_contexts(session)
            project = contexts["project"]
            _write_meta_rows(
                session,
                project_id=project.id,
                meta_context_id=contexts["meta"].id,
                rows=[
                    _meta_row(
                        kind="job_state",
                        request=request,
                        job_key="current",
                        status="success",
                        apps_upserted=result.apps_upserted,
                        tools_upserted=result.tools_upserted,
                        apps_pruned=result.apps_pruned,
                        tools_pruned=result.tools_pruned,
                        skipped_batches=result.skipped_batches,
                        completed_batches=result.completed_batches,
                    ),
                ],
            )
            session.commit()
        return result

    def sync_batch(batch_index: int, batch_slugs: list[str]) -> dict[str, Any]:
        try:
            with session_factory() as session:
                contexts = ensure_builtins_catalog_contexts(session)
                project = contexts["project"]
                if _checkpoint_complete(
                    session,
                    meta_context_id=contexts["meta"].id,
                    request=request,
                    batch_index=batch_index,
                    app_slugs=batch_slugs,
                ):
                    return {"skipped": True, "matched_app_slugs": batch_slugs}

                batch_started_at = time.perf_counter()
                body = _build_sync_payload(
                    request,
                    sync_tools=True,
                    app_slugs=[slug.upper() for slug in batch_slugs],
                )
                fetch = _fetch_provider_catalog_with_retry(session, body)
                counts = _materialize_tools(
                    session,
                    project_id=project.id,
                    tools_context_id=contexts["tools"].id,
                    meta_context_id=contexts["meta"].id,
                    request=request,
                    tools=fetch.tools,
                    prune_stale=request.prune_unlisted_apps,
                    prune_app_slugs=batch_slugs,
                )
                checkpoint_key = _checkpoint_key(
                    request,
                    batch_index=batch_index,
                    app_slugs=batch_slugs,
                )
                _write_meta_rows(
                    session,
                    project_id=project.id,
                    meta_context_id=contexts["meta"].id,
                    rows=[
                        _meta_row(
                            kind="batch_checkpoint",
                            request=request,
                            checkpoint_key=checkpoint_key,
                            status="success",
                            batch_index=batch_index,
                            app_slugs=[
                                _normalize_app_slug(slug) for slug in batch_slugs
                            ],
                            tools_upserted=counts["total"],
                            tools_pruned=counts.get("pruned", 0),
                        ),
                    ],
                )
                session.commit()
                elapsed = round(time.perf_counter() - batch_started_at, 3)
                logger.info(
                    "Builtins tool batch completed run_id=%s backend=%s batch_index=%s "
                    "app_slugs=%s tools_upserted=%s elapsed_seconds=%s",
                    request.run_id,
                    request.backend_id,
                    batch_index,
                    batch_slugs,
                    counts["total"],
                    elapsed,
                )
                return {
                    "skipped": False,
                    "inserted": counts["inserted"],
                    "updated": counts["updated"],
                    "total": counts["total"],
                    "pruned": counts.get("pruned", 0),
                    "matched_app_slugs": fetch.matched_app_slugs,
                    "skipped_apps": fetch.skipped_apps,
                    "elapsed_seconds": elapsed,
                }
        except Exception as exc:
            with session_factory() as failure_session:
                contexts = ensure_builtins_catalog_contexts(failure_session)
                project = contexts["project"]
                _write_meta_rows(
                    failure_session,
                    project_id=project.id,
                    meta_context_id=contexts["meta"].id,
                    rows=[
                        _meta_row(
                            kind="batch_checkpoint",
                            request=request,
                            checkpoint_key=_checkpoint_key(
                                request,
                                batch_index=batch_index,
                                app_slugs=batch_slugs,
                            ),
                            status="failed",
                            batch_index=batch_index,
                            app_slugs=[
                                _normalize_app_slug(slug) for slug in batch_slugs
                            ],
                            error=str(exc)[:1000],
                        ),
                    ],
                )
                failure_session.commit()
            raise

    workers = min(request.workers, len(batches))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_by_index = {
            executor.submit(sync_batch, index, batch): index
            for index, batch in enumerate(batches, start=1)
        }
        for future in as_completed(future_by_index):
            batch_result = future.result()
            if batch_result.get("skipped"):
                result.skipped_batches += 1
                continue
            result.completed_batches += 1
            result.tools_inserted += int(batch_result.get("inserted", 0) or 0)
            result.tools_updated += int(batch_result.get("updated", 0) or 0)
            result.tools_upserted += int(batch_result.get("total", 0) or 0)
            result.tools_pruned += int(batch_result.get("pruned", 0) or 0)
            result.skipped_apps.extend(batch_result.get("skipped_apps") or [])

    result.status = "success"
    result.elapsed_seconds = round(time.perf_counter() - started_at, 3)
    result.diagnostics["tool_phase_seconds"] = round(
        max(
            0.0,
            result.elapsed_seconds - result.diagnostics.get("app_phase_seconds", 0),
        ),
        3,
    )
    with session_factory() as session:
        contexts = ensure_builtins_catalog_contexts(session)
        project = contexts["project"]
        _write_meta_rows(
            session,
            project_id=project.id,
            meta_context_id=contexts["meta"].id,
            rows=[
                _meta_row(
                    kind="job_state",
                    request=request,
                    job_key="current",
                    status="success",
                    apps_upserted=result.apps_upserted,
                    tools_upserted=result.tools_upserted,
                    apps_pruned=result.apps_pruned,
                    tools_pruned=result.tools_pruned,
                    skipped_batches=result.skipped_batches,
                    completed_batches=result.completed_batches,
                ),
            ],
        )
        session.commit()
    return result
