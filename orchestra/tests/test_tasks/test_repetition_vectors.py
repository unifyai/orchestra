"""Golden regression corpus for the recurrence arithmetic.

This was a cross-repo drift guard: the arithmetic existed twice, here and in
``unify/task_scheduler/types/repetition.py``, and both repos checked a
byte-identical corpus so a semantic change ported to only one side failed.

Successor projection is now owned solely by Orchestra — unify no longer
computes slots, and its copy is deleted — so the corpus guards this one
implementation against accidental change instead. The vectors were verified
identical across both implementations at the point the duplicate was removed,
so nothing about the semantics moved with the ownership.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from orchestra.services.task_repetition import (
    RepeatPattern,
    deterministic_jitter_seconds,
    next_repeated_start_at,
)

_VECTORS = json.loads(
    Path(__file__)
    .with_name("repeat_projection_vectors.json")
    .read_text(
        encoding="utf-8",
    ),
)


@pytest.mark.parametrize(
    "case",
    _VECTORS["next_occurrence"],
    ids=lambda case: case["name"],
)
def test_next_occurrence_matches_shared_vector(case: dict) -> None:
    result = next_repeated_start_at(
        previous_start=datetime.fromisoformat(case["previous_start"]),
        patterns=[RepeatPattern.model_validate(p) for p in case["patterns"]],
        current_occurrence_index=case.get("current_occurrence_index", 0),
        now=datetime.fromisoformat(case["now"]),
    )
    assert (result.isoformat() if result else None) == case["expected"]


@pytest.mark.parametrize(
    "case",
    _VECTORS["dispatch_jitter"],
    ids=lambda case: case["name"],
)
def test_dispatch_jitter_matches_shared_vector(case: dict) -> None:
    offset = deterministic_jitter_seconds(
        task_id=case["task_id"],
        slot=datetime.fromisoformat(case["slot"]),
        patterns=[RepeatPattern.model_validate(p) for p in case["patterns"]],
    )
    assert offset == case["expected"]


class TestScheduleTimezone:
    """A clock reading needs to say whose clock it is.

    Every bundled workflow plants a bare wall-clock time meaning a human hour
    -- "before stand-up", "end of day" -- and every one of them was resolved
    as UTC. For a reader five hours east a 17:30 end-of-day log fired at
    22:30; for one eight hours west, the previous morning.
    """

    WEEKDAYS = ["MO", "TU", "WE", "TH", "FR"]

    def test_omitting_the_zone_still_means_utc(self):
        """Every schedule written before the field existed meant UTC by omission."""
        from datetime import datetime, timezone

        from orchestra.services.task_repetition import (
            RepeatPattern,
            next_repeated_start_at,
        )

        pattern = RepeatPattern(
            frequency="weekly",
            weekdays=self.WEEKDAYS,
            time_of_day="08:30",
        )
        nxt = next_repeated_start_at(
            previous_start=datetime(2026, 8, 17, 8, 30, tzinfo=timezone.utc),
            patterns=[pattern],
        )

        assert nxt == datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc)

    def test_a_zoned_slot_fires_at_that_local_hour(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        from orchestra.services.task_repetition import (
            RepeatPattern,
            next_repeated_start_at,
        )

        pattern = RepeatPattern(
            frequency="weekly",
            weekdays=self.WEEKDAYS,
            time_of_day="08:30",
            timezone="Asia/Karachi",
        )
        nxt = next_repeated_start_at(
            previous_start=datetime(2026, 8, 17, 8, 30, tzinfo=timezone.utc),
            patterns=[pattern],
        )

        # 08:30 in UTC+5 is 03:30Z — the hour the user means, not the digits.
        assert nxt == datetime(2026, 8, 18, 3, 30, tzinfo=timezone.utc)
        assert nxt.astimezone(ZoneInfo("Asia/Karachi")).strftime("%H:%M") == "08:30"

    def test_the_local_hour_survives_a_dst_boundary(self):
        """Adding 24h to a UTC instant would slide the slot by the offset change.

        A briefing set for 08:30 would start arriving at 07:30 for half the
        year, which is the failure a naive timedelta produces.
        """
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        from orchestra.services.task_repetition import (
            RepeatPattern,
            next_repeated_start_at,
        )

        zone = ZoneInfo("America/New_York")
        pattern = RepeatPattern(
            frequency="daily",
            time_of_day="08:30",
            timezone="America/New_York",
        )

        # US DST ends 2026-11-01. The occurrence before it is 08:30 EDT
        # (12:30Z) and the one after is 08:30 EST (13:30Z), so crossing costs
        # 25 absolute hours. A naive `+ timedelta(days=1)` would hold 12:30Z
        # and land at 07:30 local.
        before = datetime(2026, 10, 31, 12, 30, tzinfo=timezone.utc)
        after = next_repeated_start_at(previous_start=before, patterns=[pattern])

        assert before.astimezone(zone).strftime("%H:%M") == "08:30"
        assert after.astimezone(zone).strftime("%H:%M") == "08:30"
        assert after == datetime(2026, 11, 1, 13, 30, tzinfo=timezone.utc)
        assert (after - before).total_seconds() == 25 * 3600

    def test_an_unresolvable_zone_is_refused_at_the_boundary(self):
        """A typo must fail loudly, not fire an hour wrong forever."""
        import pytest

        from orchestra.services.task_repetition import RepeatPattern

        with pytest.raises(ValueError, match="Unknown IANA timezone"):
            RepeatPattern(
                frequency="daily",
                time_of_day="08:30",
                timezone="Asia/Karach",  # missing the final 'i'
            )
