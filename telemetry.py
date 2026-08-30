"""OpenTelemetry setup: traces + metrics + logs.

Exporters are chosen by env var so this runs with or without a collector:

  OTEL_CONSOLE=1   -> print spans/metrics to stdout (default when no OTLP endpoint)
  OTEL_OTLP=1      -> also ship to OTLP gRPC at OTEL_EXPORTER_OTLP_ENDPOINT
                      (default http://localhost:4317, i.e. Jaeger / the Collector)
  LOG_LEVEL=DEBUG  -> root log level (default INFO)

Logs are the odd one out: they always go to stdout in a human-readable form,
because that is where you look for them, and are *additionally* shipped over
OTLP when it's on.
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "lean-shop-api")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Ship to a collector only when asked; fall back to the console otherwise so
# the app is useful before you've started any backend.
USE_OTLP = os.getenv("OTEL_OTLP", "0") == "1"
USE_CONSOLE = os.getenv("OTEL_CONSOLE", "0" if USE_OTLP else "1") == "1"

_started = False


def _resource() -> Resource:
    # A resource describes *who* is emitting the telemetry. Every span, metric
    # and log carries it, so this is how a backend groups data per service.
    return Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": "0.1.0",
            "deployment.environment": os.getenv("ENV", "local"),
        }
    )


class _TraceFormatter(logging.Formatter):
    """Appends the current trace id to stdout lines.

    Exported logs get trace/span ids attached by OTel itself. This is the
    terminal equivalent: an id you can paste into Tempo or Jaeger to see the
    trace the line was written inside.
    """

    # formatMessage, not format: this has to land at the end of the message
    # line, not after a traceback where it would read as part of the error.
    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            line = f"{line} [trace_id={ctx.trace_id:032x}]"
        return line


def _setup_logging(resource: Resource) -> None:
    """Wire the stdlib `logging` module up to both stdout and OTLP.

    Nothing in the app imports OpenTelemetry to write a log line: code calls
    `logging.getLogger(...).info(...)` as usual, and the handler below turns
    that into a log record correlated with whatever span is current.

    There is deliberately no ConsoleLogExporter, unlike traces and metrics:
    logs already go to stdout, so it would print every line twice.
    """
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    console = logging.StreamHandler()
    console.setFormatter(_TraceFormatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    if USE_OTLP:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )

        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True)
            )
        )
        set_logger_provider(provider)
        # No level of its own: it defers to the root logger set above.
        root.addHandler(LoggingHandler(logger_provider=provider))

    # Chatty libraries whose log lines only repeat a span you already have.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # uvicorn installs its own handlers and turns propagation off, so its
    # startup and access lines would never reach the handlers above — they'd
    # be missing from the backend exactly when you need them.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def _setup_traces(resource: Resource) -> None:
    provider = TracerProvider(resource=resource)

    if USE_CONSOLE:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    if USE_OTLP:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True)
            )
        )

    trace.set_tracer_provider(provider)


def _setup_metrics(resource: Resource) -> None:
    readers = []

    if USE_CONSOLE:
        readers.append(
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(), export_interval_millis=15000
            )
        )

    if USE_OTLP:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )

        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
                export_interval_millis=10000,
            )
        )

    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))


def setup_telemetry() -> None:
    """Call once, before creating the FastAPI app."""
    global _started
    if _started:
        # `uvicorn --reload` and tests can import the app module twice; setting
        # the providers again would only log a warning and duplicate handlers.
        return
    _started = True

    resource = _resource()
    _setup_traces(resource)
    _setup_metrics(resource)
    _setup_logging(resource)


# Handles the app grabs after setup_telemetry() has run.
tracer = trace.get_tracer("lean.shop")
meter = metrics.get_meter("lean.shop")
