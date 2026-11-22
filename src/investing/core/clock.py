"""Indian market dates come from an IST clock, not the host's."""
from datetime import date, datetime, timedelta, timezone

# A fixed offset: India has no DST, and this needs no tzdata.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def today_ist() -> date:
    """Today's date in India."""
    return datetime.now(IST).date()


def month_ends(last: date, count: int) -> list[date]:
    """The month ends of the ``count`` months up to and including the month of ``last``, oldest first."""
    index = last.year * 12 + last.month - 1
    return [month_end((index - back) // 12, (index - back) % 12 + 1) for back in range(count - 1, -1, -1)]


def month_end(year: int, month: int) -> date:
    """The last calendar day of the month."""
    return date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
