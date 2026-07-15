import logging
from typing import Any

from app.core.config import Settings
from app.core.trace_id import get_trace_id

_base_log_record_factory = logging.getLogRecordFactory()


def _trace_log_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """为所有日志记录注入当前请求 Trace ID。"""
    record = _base_log_record_factory(*args, **kwargs)
    setattr(record, "trace_id", get_trace_id() or "-")
    return record


def configure_logging(settings: Settings) -> None:
    logging.setLogRecordFactory(_trace_log_record_factory)
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] [trace_id=%(trace_id)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
