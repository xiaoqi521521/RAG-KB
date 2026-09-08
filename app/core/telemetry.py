from __future__ import annotations

import logging
from typing import Protocol

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)


class MetricsSettings(Protocol):
    enable_metrics: bool
    app_name: str
    app_env: str


def init_metrics(settings: MetricsSettings) -> MeterProvider | None:
    """按配置初始化进程级 OpenTelemetry MeterProvider 并注册到全局。

    指标统一通过 OTel API 记录，PrometheusMetricReader 把全部指标
    转写到 prometheus_client 默认 registry，由 /metrics 端点统一暴露。
    关闭 scope_info 避免每个样本附加与业务无关的 otel_scope_* 标签。
    """
    if not settings.enable_metrics:
        return None

    provider = MeterProvider(
        resource=Resource.create(
            {
                "service.name": settings.app_name,
                "deployment.environment.name": settings.app_env,
            }
        ),
        metric_readers=[PrometheusMetricReader(scope_info_enabled=False)],
    )
    metrics.set_meter_provider(provider)
    return provider


def shutdown_metrics(provider: MeterProvider | None) -> None:
    """关闭已初始化的指标提供器，未启用时直接返回。"""
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenTelemetry metrics shutdown failed: error=%s", exc)
