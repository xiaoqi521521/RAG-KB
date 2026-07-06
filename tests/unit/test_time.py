from datetime import datetime
from zoneinfo import ZoneInfo


def test_shanghai_now_naive_returns_local_naive_datetime() -> None:
    from app.core.time import shanghai_now_naive

    before = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    current = shanghai_now_naive()
    after = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)

    assert current.tzinfo is None
    assert before <= current <= after
