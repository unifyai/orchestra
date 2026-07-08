#!/usr/bin/env python3
"""Generate the Pipedream allowlist minus Composio-covered apps.

Fetches live provider catalogs, applies slug/display-name alias resolution,
optionally verifies Pipedream action components exist, and writes
``deploy/integrations/pipedream_allowlist.toml``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestra.integrations.provider_resolution import (  # noqa: E402
    CatalogAppRef,
    composio_covered_keys,
    logical_app_key,
    normalize_display_name,
    slug_variants,
)
from orchestra.integrations.providers.composio import (  # noqa: E402
    ComposioProviderAdapter,
)
from orchestra.integrations.providers.pipedream import (  # noqa: E402
    PipedreamProviderAdapter,
    _pipedream_app_slug,
)

DEFAULT_OUTPUT = REPO_ROOT / "deploy" / "integrations" / "pipedream_allowlist.toml"


@dataclass(frozen=True)
class MatchReason:
    pipedream_slug: str
    composio_slug: str
    reason: str


def _fetch_composio() -> list[dict]:
    adapter = ComposioProviderAdapter(
        api_key=os.environ.get("COMPOSIO_API_KEY"),
        max_pages=500,
        max_items=100_000,
    )
    return [
        dict(entry)
        for entry in adapter.list_app_entries(include_all=True, include_detail=False)
    ]


def _fetch_pipedream(*, verify_components: bool) -> list[dict]:
    adapter = PipedreamProviderAdapter(max_pages=1000, max_items=100_000)
    entries = [
        {
            "backend_id": "pipedream",
            "canonical_app_slug": slug,
            "display_name": app.get("name") or slug.replace("_", " ").title(),
            "provider_app_id": str(
                app.get("id") or app.get("name_slug") or app.get("slug") or slug,
            ),
            "description": app.get("description"),
        }
        for app in adapter.list_apps(has_components=True)
        for slug in [_pipedream_app_slug(app)]
    ]
    if not verify_components:
        return entries
    verified: list[dict] = []
    for entry in entries:
        slug = str(entry.get("canonical_app_slug") or "")
        provider_app_id = str(entry.get("provider_app_id") or slug)
        try:
            tools = adapter.list_tool_entries(
                app_slug=slug,
                provider_app_id=provider_app_id,
                limit=1,
            )
        except Exception:
            tools = []
        if tools:
            verified.append(entry)
    return verified


def _composio_indexes(
    composio_entries: list[dict],
) -> tuple[set[str], dict[str, set[str]]]:
    keys = composio_covered_keys(composio_entries)
    by_display: dict[str, set[str]] = defaultdict(set)
    for entry in composio_entries:
        ref = CatalogAppRef.from_entry(entry)
        keys.add(ref.logical_key)
        keys.update(slug_variants(ref.canonical_app_slug))
        display = normalize_display_name(ref.display_name)
        if display:
            by_display[display].add(ref.canonical_app_slug)
    return keys, dict(by_display)


def _match_pipedream_to_composio(
    pipedream_entries: list[dict],
    composio_entries: list[dict],
) -> tuple[list[dict], list[MatchReason]]:
    composio_keys, composio_by_display = _composio_indexes(composio_entries)
    composio_slugs = {
        CatalogAppRef.from_entry(entry).canonical_app_slug for entry in composio_entries
    }
    kept: list[dict] = []
    matches: list[MatchReason] = []

    for entry in pipedream_entries:
        ref = CatalogAppRef.from_entry(entry)
        pd_slug = ref.canonical_app_slug
        reason: str | None = None
        composio_slug = ""

        if pd_slug in composio_slugs:
            reason = "exact_slug"
            composio_slug = pd_slug
        elif ref.logical_key in composio_keys:
            reason = "logical_key"
            composio_slug = ref.logical_key
        elif slug_variants(pd_slug) & composio_keys:
            reason = "slug_variant"
            composio_slug = sorted(slug_variants(pd_slug) & composio_keys)[0]
        else:
            display = normalize_display_name(ref.display_name)
            if display and display in composio_by_display:
                reason = "display_name"
                composio_slug = sorted(composio_by_display[display])[0]
            else:
                display_tokens = set(display.split()) if display else set()
                if len(display_tokens) >= 2:
                    for composio_display, slugs in composio_by_display.items():
                        composio_tokens = set(composio_display.split())
                        if not composio_tokens:
                            continue
                        overlap = display_tokens & composio_tokens
                        if len(overlap) >= max(
                            2,
                            min(len(display_tokens), len(composio_tokens)) - 1,
                        ):
                            reason = "display_token_overlap"
                            composio_slug = sorted(slugs)[0]
                            break

        if reason:
            matches.append(
                MatchReason(
                    pipedream_slug=pd_slug,
                    composio_slug=composio_slug,
                    reason=reason,
                ),
            )
            continue
        kept.append(entry)

    return kept, matches


def _write_allowlist(path: Path, slugs: list[str], *, metadata: dict) -> None:
    lines = [
        "schema_version = 1",
        "# Generated by scripts/generate_pipedream_allowlist.py",
        "# Composio is preferred; apps covered by Composio are excluded automatically.",
        "",
        "[metadata]",
    ]
    for key, value in sorted(metadata.items()):
        if isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f"{key} = {json.dumps(value)}")
    lines.extend(["", "app_slugs = ["])
    for slug in slugs:
        lines.append(f'  "{slug}",')
    lines.append("]")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Allowlist TOML output path",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional JSON audit report path",
    )
    parser.add_argument(
        "--verify-components",
        action="store_true",
        help="Keep only Pipedream apps with at least one fetchable action component",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary without writing files",
    )
    args = parser.parse_args()

    if not os.environ.get("COMPOSIO_API_KEY"):
        print("COMPOSIO_API_KEY is required", file=sys.stderr)
        return 1
    if not (
        os.environ.get("PIPEDREAM_ACCESS_TOKEN")
        or (
            os.environ.get("PIPEDREAM_CLIENT_ID")
            and os.environ.get("PIPEDREAM_CLIENT_SECRET")
        )
    ):
        print(
            "PIPEDREAM_ACCESS_TOKEN or PIPEDREAM_CLIENT_ID/SECRET is required",
            file=sys.stderr,
        )
        return 1

    composio_entries = _fetch_composio()
    pipedream_entries = _fetch_pipedream(verify_components=args.verify_components)
    candidates, suppressed = _match_pipedream_to_composio(
        pipedream_entries,
        composio_entries,
    )
    slugs = sorted(
        {
            CatalogAppRef.from_entry(entry).canonical_app_slug
            for entry in candidates
            if CatalogAppRef.from_entry(entry).canonical_app_slug
        },
    )

    metadata = {
        "composio_app_count": len(composio_entries),
        "pipedream_app_count": len(pipedream_entries),
        "suppressed_overlap_count": len(suppressed),
        "allowlist_count": len(slugs),
        "verify_components": args.verify_components,
    }
    report = {
        "metadata": metadata,
        "allowlist": slugs,
        "suppressed_matches": [
            {
                "pipedream_slug": m.pipedream_slug,
                "composio_slug": m.composio_slug,
                "reason": m.reason,
            }
            for m in sorted(suppressed, key=lambda item: item.pipedream_slug)
        ],
    }

    print(json.dumps(metadata, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote report to {args.report}")

    if args.dry_run:
        print(f"Would write {len(slugs)} slugs to {args.output}")
        return 0

    _write_allowlist(args.output, slugs, metadata=metadata)
    print(f"Wrote allowlist to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
