import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Asia/Tokyo")
WORKDAY_ROLLOVER_HOUR = 6


def normalize_iso_datetime(
    value: str, 
    *, 
    default_tz=timezone.utc
) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("`time` must be an ISO 8601 string") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def parse_clocked_time(
    value: Any, 
    *, 
    local_tz=LOCAL_TZ
) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz)


def is_allowed_clock_time(
    dt: datetime,
    *,
    tz=LOCAL_TZ,
    start_hour=23,
    rollover_hour=WORKDAY_ROLLOVER_HOUR,
) -> bool:
    local_datetime = dt.astimezone(tz)
    hmss = (
        local_datetime.hour,
        local_datetime.minute,
        local_datetime.second,
        local_datetime.microsecond,
    )
    return local_datetime.hour >= start_hour or hmss <= (rollover_hour, 0, 0, 0)


def work_day(
    local_dt: datetime,
    *, 
    rollover_hour=WORKDAY_ROLLOVER_HOUR
) -> date:
    hmss = (local_dt.hour, local_dt.minute, local_dt.second, local_dt.microsecond)
    if hmss <= (rollover_hour, 0, 0, 0):
        local_dt = local_dt - timedelta(days=1)
    return local_dt.date()


def work_date_bucket(
    dt: datetime,
    *,
    tz=LOCAL_TZ,
    rollover_hour=WORKDAY_ROLLOVER_HOUR,
) -> str:
    local_datetime = dt.astimezone(tz)
    work_date = work_day(local_datetime, rollover_hour=rollover_hour)
    return work_date.strftime("%Y%m%d")


# TODO: ここの処理がいまいちピンと来ていないので、しっかり見直す必要あり
def combine_work_datetime(
    local_time: datetime | None,
    date_bucket: Any,
    *,
    tz=LOCAL_TZ,
    rollover_hour=WORKDAY_ROLLOVER_HOUR,
) -> datetime | None:
    bucket_date = datetime.strptime(date_bucket, "%Y%m%d").date()
    if local_time:
        if bucket_date:
            return datetime.combine(bucket_date, local_time.timetz())
        local_dt = local_time.astimezone(tz)
        return datetime.combine(work_day(local_dt, rollover_hour=rollover_hour), local_time.timetz())
    if bucket_date:
        return datetime.combine(bucket_date, time(0, 0, tzinfo=tz))
    return None
