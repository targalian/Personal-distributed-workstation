"""UTC due instants for interval and local daily schedules, including DST transitions."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math


def validate_spec(value: dict, now: float) -> dict:
    if not isinstance(value, dict):
        raise ValueError('schedule spec must be an object')
    name = str(value.get('timezone', 'UTC'))
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('无效IANA时区') from exc
    kind = value.get('kind', 'daily')
    result = dict(kind=kind, timezone=name)
    if kind == 'interval':
        seconds = int(value.get('seconds', 3600))
        if not 60 <= seconds <= 366 * 86400:
            raise ValueError('间隔需为60秒至366天')
        anchor = float(value.get('anchor', now))
        if not math.isfinite(anchor):
            raise ValueError('无效起始时间')
        return dict(result, seconds=seconds, anchor=anchor)
    if kind != 'daily':
        raise ValueError('仅支持daily或interval')
    hour, minute = int(value.get('hour', 9)), int(value.get('minute', 0))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('无效当地时间')
    return dict(result, hour=hour, minute=minute)


def _daily(spec: dict, day: object) -> float | None:
    zone = ZoneInfo(spec['timezone'])
    local = datetime(day.year, day.month, day.day, spec['hour'], spec['minute'])
    instant = local.replace(tzinfo=zone, fold=0).timestamp()
    # Spring-forward gaps do not run. Repeated autumn wall times run once, at fold=0.
    if datetime.fromtimestamp(instant, zone).replace(tzinfo=None) != local:
        return None
    return instant


def next_due(spec: dict, after: float) -> float:
    if spec['kind'] == 'interval':
        return spec['anchor'] + max(1, math.floor((after - spec['anchor']) / spec['seconds']) + 1) * spec['seconds']
    day = datetime.fromtimestamp(after, ZoneInfo(spec['timezone'])).date()
    for offset in range(4):
        candidate = _daily(spec, day + timedelta(days=offset))
        if candidate is not None and candidate > after:
            return candidate
    raise ValueError('无法解析下一次当地时间')


def latest_due(spec: dict, first_due: float, now: float) -> float:
    if spec['kind'] == 'interval':
        return first_due + max(0, math.floor((now - first_due) / spec['seconds'])) * spec['seconds']
    day = datetime.fromtimestamp(now, ZoneInfo(spec['timezone'])).date()
    for offset in range(4):
        candidate = _daily(spec, day - timedelta(days=offset))
        if candidate is not None and first_due <= candidate <= now:
            return candidate
    return first_due
