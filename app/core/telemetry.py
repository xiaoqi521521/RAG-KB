from __future__ import annotations

import logging
from typing import Protocol

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)


class MetricsSettings(Protocol):
    enable_metrics: bool
    app_name: str
    app_env: str


def init_metrics(settings: MetricsSettings) -> MeterProvider | None:
    """按配置初始化进程级 OpenTelemetry MeterProvider。

    本阶段不配置 MetricReader 或 Exporter，控制台观察由 TokenMetrics 即时日志承担。
    """
    if not settings.enable_metrics:
        return None

    return MeterProvider(
        resource=Resource.create(
            {
                "service.name": settings.app_name,
                "deployment.environment.name": settings.app_env,
            }
        )
    )


def shutdown_metrics(provider: MeterProvider | None) -> None:
    """关闭已初始化的指标提供器，未启用时直接返回。"""
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenTelemetry metrics shutdown failed: error=%s", exc)
