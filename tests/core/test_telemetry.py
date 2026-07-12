from __future__ import annotations

from types import SimpleNamespace

from opentelemetry.sdk.metrics import MeterProvider

from app.core.telemetry import init_metrics, shutdown_metrics


def test_init_metrics_returns_none_when_disabled() -> None:
    settings = SimpleNamespace(enable_metrics=False, app_name="rag-kb", app_env="test")

    assert init_metrics(settings) is None


def test_init_metrics_creates_sdk_provider_without_exporter() -> None:
    settings = SimpleNamespace(enable_metrics=True, app_name="rag-kb", app_env="test")

    provider = init_metrics(settings)

    assert isinstance(provider, MeterProvider)
    assert provider._sdk_config.resource.attributes["service.name"] == "rag-kb"
    assert provider._sdk_config.resource.attributes["deployment.environment.name"] == "test"
    provider.get_meter("tests.telemetry").create_counter("tests.counter").add(1)
    provider.shutdown()


def test_shutdown_metrics_closes_initialized_provider() -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self) -> None:
            self.calls += 1

    provider = FakeProvider()

    shutdown_metrics(provider)

    assert provider.calls == 1
