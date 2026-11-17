import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from app.config import get_settings

_done = False


def setup_tracing(service: str) -> None:
    """OpenTelemetry tracing. Spans are exported to Jaeger (OTLP/HTTP) when OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    global _done
    if _done:
        return
    _done = True
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    ep = get_settings().otel_exporter_otlp_endpoint
    if ep:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ep.rstrip("/") + "/v1/traces")))
        except Exception as e:  # tracing must never block startup
            logging.getLogger(__name__).warning("OTLP exporter unavailable: %s", type(e).__name__)
    trace.set_tracer_provider(provider)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
