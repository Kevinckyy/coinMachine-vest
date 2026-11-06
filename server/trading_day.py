"""Daily PnL window, delimited by 17:00 in New York (not UTC midnight)."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    NEW_YORK = ZoneInfo('America/New_York')
except ZoneInfoNotFoundError:
    NEW_YORK = None


def _close_timestamp(day: date) -> float:
    if NEW_YORK is not None:
        return datetime.combine(day,time(17),NEW_YORK).timestamp()
    # Standard-library-only Windows installations have no IANA database.
    # At 17:00 the US DST transition has already happened that day. Use the
    # current (since 2007) US rules, independently for each interval endpoint.
    march = date(day.year,3,1)
    november = date(day.year,11,1)
    dst_start = march+timedelta(days=(6-march.weekday())%7+7)
    dst_end = november+timedelta(days=(6-november.weekday())%7)
    offset = -4 if dst_start <= day < dst_end else -5
    return datetime.combine(day,time(17),timezone(timedelta(hours=offset))).timestamp()


def trading_day_window(now: float) -> dict:
    day = datetime.fromtimestamp(now,timezone.utc).date()
    if now >= _close_timestamp(day):
        day += timedelta(days=1)
    return {'start':_close_timestamp(day-timedelta(days=1)), 'end':_close_timestamp(day),
            'label':day.isoformat(),'timezone':'America/New_York','cutoff':'17:00'}
