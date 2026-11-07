"""New York daily windows: PnL at 17:00, entry quotas and locks at 20:00."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    NEW_YORK = ZoneInfo('America/New_York')
except ZoneInfoNotFoundError:
    NEW_YORK = None


def _close_timestamp(day: date, hour=17) -> float:
    if NEW_YORK is not None:
        return datetime.combine(day,time(hour),NEW_YORK).timestamp()
    # Standard-library-only Windows installations have no IANA database.
    # At both supported cutoffs the US DST transition has already happened. Use
    # current (since 2007) US rules, independently for each interval endpoint.
    march = date(day.year,3,1)
    november = date(day.year,11,1)
    dst_start = march+timedelta(days=(6-march.weekday())%7+7)
    dst_end = november+timedelta(days=(6-november.weekday())%7)
    offset = -4 if dst_start <= day < dst_end else -5
    return datetime.combine(day,time(hour),timezone(timedelta(hours=offset))).timestamp()


def _day_window(now: float, hour: int) -> dict:
    day = datetime.fromtimestamp(now,timezone.utc).date()
    while now < _close_timestamp(day,hour):day-=timedelta(days=1)
    end_day=day+timedelta(days=1)
    return {'start':_close_timestamp(day,hour), 'end':_close_timestamp(end_day,hour),
            'label':end_day.isoformat(),'timezone':'America/New_York','cutoff':f'{hour:02}:00'}


def trading_day_window(now: float) -> dict:return _day_window(now,17)


def entry_day_window(now: float) -> dict:return _day_window(now,20)
