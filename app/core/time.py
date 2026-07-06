from datetime import datetime
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def shanghai_now_naive() -> datetime:
    """返回上海时区当前时间，并去掉时区信息以匹配数据库 TIMESTAMP 字段。

    Returns:
        不携带 tzinfo 的北京时间 datetime，用于写入现有无时区时间列。
    """
    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
