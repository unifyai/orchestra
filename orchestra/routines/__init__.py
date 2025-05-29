# Celery is optional when the test-suite runs without workers
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from celery.schedules import crontab
else:
    try:
        from celery.schedules import crontab
    except ModuleNotFoundError:  # pragma: no cover

        def crontab(*_a, **_kw):  # type: ignore
            """Tiny stub that mimics `celery.schedules.crontab`."""
            return ("stub-crontab", _a, _kw)


CELERY_BEAT_SCHEDULE = {
    # existing tasks …
    "monthly-invoice": {
        "task": "orchestra.routines.monthly_invoicer.invoice_month",
        "schedule": crontab(minute=5, hour=0, day_of_month="1"),  # 00:05 UTC on the 1st
        "args": (),  # the routine defaults to "previous month"
    },
    "billing-guard": {
        "task": "orchestra.routines.billing_guard.suspend_past_due_users",
        "schedule": crontab(hour=0, minute=10),  # every day at 00:10 UTC
        "args": (),
    },
}
