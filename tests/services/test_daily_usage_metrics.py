from __future__ import annotations

from decimal import Decimal

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.services.daily_usage_metrics import DailyUsageMetrics


def test_daily_usage_metrics_exports_service_snapshot() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    usage_metrics = DailyUsageMetrics(meter=provider.get_meter("tests.daily-usage"))
    usage_metrics.update(tokens=324_390, cost_cny=Decimal("0.6963"))

    provider.force_flush()
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    metrics = {
        metric.name: metric.data.data_points[0]
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }
    assert metrics["rag_daily_usage_tokens"].value == 324_390
    assert metrics["rag_daily_usage_tokens"].attributes == {"scope": "global"}
    assert metrics["rag_daily_usage_cost_cny"].value == 0.6963
    assert metrics["rag_daily_usage_cost_cny"].attributes == {"scope": "global"}

    provider.shutdown()
