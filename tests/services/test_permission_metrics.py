from __future__ import annotations

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader


def test_permission_metrics_record_low_cardinality_result_and_duration() -> None:
    from app.services.permission_metrics import PermissionMetrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = PermissionMetrics(meter=provider.get_meter("tests.permission-metrics"))

    metrics.record(action="read", result="allowed", source="public", elapsed_ms=12)
    provider.force_flush()

    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    recorded = [
        metric
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    ]
    assert {metric.name for metric in recorded} == {
        "rag.permission.checks",
        "rag.permission.duration",
    }
    attributes = recorded[0].data.data_points[0].attributes
    assert attributes == {"action": "read", "result": "allowed", "source": "public"}
    assert "user_id" not in attributes
    assert "kb_id" not in attributes

    provider.shutdown()
