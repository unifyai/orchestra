"""Tests for Pipedream pagination cursor parsing."""

from __future__ import annotations

from orchestra.integrations.providers.pipedream import _pipedream_cursor_page
from orchestra.integrations.providers.utils.pagination import (
    PaginationLimits,
    collect_cursor_pages,
)


def test_pipedream_cursor_page_does_not_set_misleading_total_items() -> None:
    page = _pipedream_cursor_page(
        {
            "data": [{"id": "app_1"}],
            "page_info": {"count": 100, "total_count": 100, "end_cursor": "next"},
        },
    )
    assert page.next_cursor == "next"
    assert page.total_items is None


def test_collect_cursor_pages_continues_when_page_size_equals_count() -> None:
    pages = iter(
        [
            _pipedream_cursor_page(
                {
                    "data": [{"id": "app_1"}],
                    "page_info": {
                        "count": 1,
                        "total_count": 1,
                        "end_cursor": "cursor-2",
                    },
                },
            ),
            _pipedream_cursor_page(
                {"data": [{"id": "app_2"}], "page_info": {"count": 1}},
            ),
        ],
    )

    def fetch_page(cursor: str | None, page_size: int):
        assert cursor in (None, "cursor-2")
        return next(pages)

    items = collect_cursor_pages(
        fetch_page,
        limits=PaginationLimits(
            page_size=1,
            max_page_size=100,
            max_pages=10,
            max_items=10,
        ),
    )
    assert [item["id"] for item in items] == ["app_1", "app_2"]
