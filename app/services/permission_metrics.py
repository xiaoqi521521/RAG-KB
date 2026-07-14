from __future__ import annotations

import logging

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram, Meter

logger = logging.getLogger(__name__)


class PermissionMetrics:
    """记录不含用户和资源标识的权限检查观测数据。"""

    def __init__(self, *, meter: Meter | None = None) -> None:
        """初始化权限检查 Counter 和耗时 Histogram。"""
        effective_meter = meter or metrics.get_meter("rag-kb.permissions")
        self._checks: Counter = effective_meter.create_counter(
            "rag.permission.checks",
            description="知识库权限检查次数",
        )
        self._durations: Histogram = effective_meter.create_histogram(
            "rag.permission.duration",
            unit="ms",
            description="知识库权限检查耗时",
        )

    def record(self, *, action: str, result: str, source: str, elapsed_ms: int) -> None:
        """记录一次权限检查，不让遥测故障影响访问控制。"""
        attributes = {"action": action, "result": result, "source": source}
        logger.info(
            "permission_check_completed=true action=%s result=%s source=%s elapsed_ms=%s",
            action,
            result,
            source,
            elapsed_ms,
        )
        try:
            self._checks.add(1, attributes)
            self._durations.record(elapsed_ms, attributes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("permission_metric_write_failed=true error_type=%s", type(exc).__name__)
