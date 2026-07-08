"""Cross-provider integration app resolution.

Composio is the preferred backend. Pipedream apps are gated by an explicit
allowlist and suppressed whenever Composio already covers the same logical app,
including known slug/display-name aliases.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from orchestra.integrations.providers.utils.normalization import slugify

PREFERRED_BACKEND_ORDER = ("composio", "pipedream")
DEFAULT_ALLOWLIST_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "integrations"
    / "pipedream_allowlist.toml"
)

# Hand-curated slug aliases where providers disagree on canonical slugs.
EXPLICIT_SLUG_ALIASES: dict[str, str] = {
    "microsoft_outlook": "outlook",
    "microsoft_outlook_calendar": "outlook",
    "microsoft_excel": "excel",
    "microsoft_onedrive": "onedrive",
    "microsoft_365": "office_365",
    "microsoft_365_people": "office_365",
    "microsoft_365_planner": "office_365",
    "google_calendar": "googlecalendar",
    "google_sheets": "googlesheets",
    "google_drive": "googledrive",
    "google_docs": "googledocs",
    "google_meet": "googlemeet",
    "google_forms": "googleforms",
    "google_contacts": "googlecontacts",
    "google_slides": "googleslides",
    "google_tasks": "googletasks",
    "google_chat": "googlechat",
    "airtable_oauth": "airtable",
    "databricks_oauth": "databricks",
    "gorgias_oauth": "gorgias",
    "highlevel_oauth": "highlevel",
    "sendfox_oauth": "sendfox",
    "snowflake_oauth": "snowflake",
    "apify_oauth": "apify",
    "twocaptcha": "twocaptcha",
}

_STRIP_SUFFIXES = (
    "_oauth",
    "_api",
    "_connect",
    "_integration",
    "_integrations",
    "_app",
)


def normalize_display_name(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    for suffix in (" oauth", " api", " integration"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return re.sub(r"\s+", " ", text)


def slug_variants(slug: str) -> set[str]:
    """Return slug variants that may refer to the same logical app."""

    base = slugify(slug)
    if not base:
        return set()
    variants: set[str] = {base}
    if base in EXPLICIT_SLUG_ALIASES:
        variants.add(EXPLICIT_SLUG_ALIASES[base])
    for candidate, target in EXPLICIT_SLUG_ALIASES.items():
        if target == base:
            variants.add(candidate)
    if base.startswith("microsoft_"):
        variants.add(base.removeprefix("microsoft_"))
    if base.startswith("google_"):
        variants.add(f"google{base.removeprefix('google_')}")
    for suffix in _STRIP_SUFFIXES:
        if base.endswith(suffix):
            variants.add(base[: -len(suffix)])
    compact = base.replace("_", "")
    if compact:
        variants.add(compact)
    return {slugify(value) for value in variants if value}


def logical_app_key(
    *,
    canonical_app_slug: str,
    display_name: str | None = None,
    provider_app_id: str | None = None,
) -> str:
    """Stable key for comparing coverage across providers."""

    slug_candidates = slug_variants(canonical_app_slug)
    if provider_app_id:
        slug_candidates.update(slug_variants(str(provider_app_id)))
    preferred = slugify(canonical_app_slug)
    if preferred in EXPLICIT_SLUG_ALIASES:
        return EXPLICIT_SLUG_ALIASES[preferred]
    if preferred.startswith("microsoft_"):
        return preferred.removeprefix("microsoft_")
    for suffix in _STRIP_SUFFIXES:
        if preferred.endswith(suffix):
            return preferred[: -len(suffix)]
    name_key = normalize_display_name(display_name)
    if name_key:
        name_slug = slugify(name_key)
        if name_slug in EXPLICIT_SLUG_ALIASES:
            return EXPLICIT_SLUG_ALIASES[name_slug]
        return name_slug
    return preferred


@dataclass(frozen=True)
class CatalogAppRef:
    backend_id: str
    canonical_app_slug: str
    display_name: str | None = None
    provider_app_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> CatalogAppRef:
        return cls(
            backend_id=str(entry.get("backend_id") or "provider"),
            canonical_app_slug=slugify(
                str(
                    entry.get("canonical_app_slug")
                    or entry.get("app_slug")
                    or entry.get("provider_app_id")
                    or "",
                ),
            ),
            display_name=(
                str(entry.get("display_name") or entry.get("app_display_name") or "")
                or None
            ),
            provider_app_id=(str(entry.get("provider_app_id") or "") or None),
            raw=dict(entry),
        )

    @property
    def logical_key(self) -> str:
        return logical_app_key(
            canonical_app_slug=self.canonical_app_slug,
            display_name=self.display_name,
            provider_app_id=self.provider_app_id,
        )


def composio_covered_keys(
    apps: Iterable[CatalogAppRef | Mapping[str, Any]],
) -> set[str]:
    keys: set[str] = set()
    for app in apps:
        ref = app if isinstance(app, CatalogAppRef) else CatalogAppRef.from_entry(app)
        if ref.backend_id != "composio":
            continue
        keys.add(ref.logical_key)
        keys.update(slug_variants(ref.canonical_app_slug))
    return keys


def load_pipedream_allowlist(path: Path | None = None) -> set[str]:
    allowlist_path = path or DEFAULT_ALLOWLIST_PATH
    if not allowlist_path.is_file():
        return set()
    data = tomllib.loads(allowlist_path.read_text(encoding="utf-8"))
    return {
        slugify(str(slug)) for slug in data.get("app_slugs") or [] if str(slug).strip()
    }


def pipedream_allowlist_enabled(path: Path | None = None) -> bool:
    allowlist_path = path or DEFAULT_ALLOWLIST_PATH
    if not allowlist_path.is_file():
        return False
    data = tomllib.loads(allowlist_path.read_text(encoding="utf-8"))
    return bool(data.get("app_slugs"))


def should_sync_pipedream_app(
    entry: Mapping[str, Any] | CatalogAppRef,
    *,
    composio_keys: set[str],
    allowlist: set[str] | None = None,
    allowlist_path: Path | None = None,
) -> bool:
    ref = entry if isinstance(entry, CatalogAppRef) else CatalogAppRef.from_entry(entry)
    if ref.backend_id != "pipedream":
        return True
    allowed = (
        allowlist if allowlist is not None else load_pipedream_allowlist(allowlist_path)
    )
    if not allowed:
        return False
    if ref.canonical_app_slug not in allowed:
        return False
    if ref.logical_key in composio_keys:
        return False
    if slug_variants(ref.canonical_app_slug) & composio_keys:
        return False
    return True


def filter_pipedream_app_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    composio_entries: Sequence[Mapping[str, Any]],
    allowlist: set[str] | None = None,
    allowlist_path: Path | None = None,
) -> list[dict[str, Any]]:
    composio_keys = composio_covered_keys(composio_entries)
    allowed = (
        allowlist if allowlist is not None else load_pipedream_allowlist(allowlist_path)
    )
    kept: list[dict[str, Any]] = []
    for entry in entries:
        if str(entry.get("backend_id") or "pipedream") != "pipedream":
            kept.append(dict(entry))
            continue
        if should_sync_pipedream_app(
            entry,
            composio_keys=composio_keys,
            allowlist=allowed,
        ):
            kept.append(dict(entry))
    return kept


def backend_preference_rank(backend_id: str) -> int:
    try:
        return PREFERRED_BACKEND_ORDER.index(backend_id)
    except ValueError:
        return len(PREFERRED_BACKEND_ORDER)


def resolve_public_catalog_apps(
    apps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return one catalog row per logical app; Composio wins ties."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in apps:
        ref = CatalogAppRef.from_entry(entry)
        grouped.setdefault(ref.logical_key, []).append(dict(entry))
    resolved: list[dict[str, Any]] = []
    for key in sorted(grouped):
        rows = grouped[key]
        rows.sort(
            key=lambda row: (
                backend_preference_rank(str(row.get("backend_id") or "provider")),
                str(row.get("display_name") or row.get("canonical_app_slug") or ""),
            ),
        )
        resolved.append(rows[0])
    return resolved
