"""Safe pagination helpers for provider catalog APIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PaginationLimits:
    """Hard stops for provider pagination loops."""

    page_size: int
    max_page_size: int
    max_pages: int = 500
    max_items: int = 100_000

    @property
    def bounded_page_size(self) -> int:
        return max(1, min(self.page_size, self.max_page_size))


@dataclass(frozen=True)
class CursorPage:
    """Provider-neutral page payload."""

    items: list[dict[str, Any]]
    next_cursor: str | None = None
    total_items: int | None = None
    total_pages: int | None = None
    current_page: int | None = None


class ProviderPaginationError(RuntimeError):
    """Raised when a provider returns unsafe or malformed pagination metadata."""


def collect_cursor_pages(
    fetch_page: Callable[[str | None, int], CursorPage],
    *,
    limits: PaginationLimits,
) -> list[dict[str, Any]]:
    """Collect cursor pages with finite, documented stopping conditions."""

    items: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for page_number in range(1, limits.max_pages + 1):
        page = fetch_page(cursor, limits.bounded_page_size)
        if not isinstance(page.items, list):
            raise ProviderPaginationError("Provider page items must be a list.")
        items.extend(item for item in page.items if isinstance(item, dict))

        if len(items) >= limits.max_items:
            return items[: limits.max_items]
        if page.total_items is not None and len(items) >= page.total_items:
            return items[: page.total_items]
        if (
            page.total_pages is not None
            and page.current_page is not None
            and page.current_page >= page.total_pages
        ):
            return items
        if not page.next_cursor:
            return items
        if page.next_cursor in seen_cursors or page.next_cursor == cursor:
            raise ProviderPaginationError(
                "Provider returned a repeated pagination cursor.",
            )
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor

    raise ProviderPaginationError(
        f"Provider pagination exceeded {limits.max_pages} pages.",
    )


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
