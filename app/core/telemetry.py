from __future__ import annotations

import logging
from typing import Protocol

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from prometheus_client import PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR, REGISTRY

logger = logging.getLogger(__name__)


class MetricsSettings(Protocol):
    enable_metrics: bool
    app_name: str
    app_env: str


def init_metrics(settings: MetricsSettings) -> MeterProvider | None:
    """按配置初始化进程级 OpenTelemetry MeterProvider 并注册到全局。

    指标统一通过 OTel API 记录，PrometheusMetricReader 把全部指标
    转写到 prometheus_client 默认 registry，由 /metrics 端点统一暴露。
    关闭 scope_info 与 target_info，并注销 GC/平台/进程默认 collector，
    让 /metrics 只保留业务指标，避免运行时噪音。
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
        metric_readers=[
            PrometheusMetricReader(scope_info_enabled=False, disable_target_info=True),
        ],
    )
    for collector in (GC_COLLECTOR, PLATFORM_COLLECTOR, PROCESS_COLLECTOR):
        try:
            REGISTRY.unregister(collector)
        except Exception as exc:  # noqa: BLE001
            logger.warning("default collector unregister failed: %s", type(exc).__name__)
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
