from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from algoflat.time_util import IST, age_seconds, ensure_ist, from_epoch_ms, now_ist


def test_now_ist_is_ist_aware() -> None:
    ts = now_ist()
    assert ts.tzinfo == IST


def test_age_seconds_ignores_tz_label() -> None:
    utc = datetime.now(tz=timezone.utc)
    ist = utc.astimezone(IST)
    assert age_seconds(utc, now=ist) is not None
    assert age_seconds(utc, now=ist) < 1.0


def test_from_epoch_ms_returns_ist() -> None:
    ms = int(datetime(2026, 8, 28, 9, 15, tzinfo=IST).timestamp() * 1000)
    ts = from_epoch_ms(ms)
    assert ts.tzinfo == IST
    assert ts.hour == 9
    assert ts.minute == 15


def test_ensure_ist_naive_treated_as_ist() -> None:
    naive = datetime(2026, 8, 28, 14, 30)
    out = ensure_ist(naive)
    assert out.tzinfo == IST
    assert out.hour == 14
