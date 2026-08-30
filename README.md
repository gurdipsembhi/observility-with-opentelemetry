# leanOpentelemetry

A tiny FastAPI "shop" API instrumented with OpenTelemetry — small enough to read
in one sitting, but it produces real multi-span traces with a network hop.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload --port 8000
```

Then, in another terminal:

```bash
curl localhost:8000/orders/1
```

With no backend configured, spans and metrics print to the console as JSON.
That is the whole point at first: **look at the raw span objects.**

## See it in a UI

```bash
open -a Docker                    # macOS: start the daemon if it isn't running
docker compose up -d              # Grafana + Tempo + Prometheus + Loki
OTEL_OTLP=1 .venv/bin/uvicorn main:app --port 8000
./load.sh                         # in another terminal, generates traffic
```

Grafana: http://localhost:3000 (no login) -> Explore -> Tempo -> Search ->
service `lean-shop-api`.

Full walkthrough, plus the Jaeger and standalone-Collector setups:
**[RUNNING.md](RUNNING.md)**.

Env vars: `OTEL_OTLP=1` (send to a backend), `OTEL_CONSOLE=1` (print; default when
OTLP is off — set both to do both), `OTEL_EXPORTER_OTLP_ENDPOINT` (default
`http://localhost:4317`), `LOG_LEVEL` (default `INFO`).

Logs always go to stdout, and are shipped over OTLP as well when `OTEL_OTLP=1`.
Every line is stamped with the trace it happened inside, so `order not found
[trace_id=27534cb6...]` in the terminal is one search away from the trace.

## Endpoints

| Endpoint | What it teaches |
|---|---|
| `GET /orders/{id}` | The main one: nested spans, attributes, events, a real HTTP hop |
| `GET /internal/inventory/{sku}` | Stands in for a downstream service |
| `GET /external/ip` | A client span to a third party (api.github.com) |
| `GET /orders/9` | 404 path — span with ERROR status |
| `GET /boom` | Unhandled exception — recorded exception on the span |
| `GET /health` | Boring baseline trace |

## The shape of one `/orders/1` trace

```
GET /orders/{order_id}              auto  (FastAPI server span, the root)
└─ process_order                    manual
   ├─ db.query                      manual  (db.system, db.statement attrs)
   ├─ GET                           auto    (httpx client span)
   │  └─ GET /internal/inventory/{sku}   auto (server span — SAME trace id)
   │     └─ inventory.lookup        manual
   └─ price_order                   manual
```

The nesting on the last two lines is the thing to internalise: the client span's
trace context rides along in the `traceparent` HTTP header, the downstream server
picks it up, and both ends land in one trace. That is distributed tracing —
nothing more magic than a header.

## The four concepts, and where they live in the code

- **Resource** — who is emitting (`service.name`). [telemetry.py](telemetry.py) `_resource()`
- **Span** — one unit of work with a start, end, and parent. `tracer.start_as_current_span(...)` in [main.py](main.py)
- **Attributes** — the searchable dimensions on a span (`order.id`, `db.system`).
  Prefer [semantic conventions](https://opentelemetry.io/docs/specs/semconv/) for
  anything standard, so backends know what your field means.
- **Auto vs manual instrumentation** — `FastAPIInstrumentor` / `HTTPXClientInstrumentor`
  give you every request and outbound call for free; you add manual spans only for
  the business logic that matters.

Metrics come along too: `orders.processed` (counter, by outcome) and `orders.value`
(histogram, by SKU), plus HTTP duration histograms from the auto-instrumentation.

## Things to try next

1. `curl localhost:8000/boom`, then find that trace and read `exception.stacktrace`
   on the span.
2. Add an attribute you'd actually want in an incident (`customer.tier`, say) and
   filter for it in Jaeger.
3. Run two copies on different ports and have one call the other — the trace should
   still be one trace.
4. `curl localhost:8000/orders/99`, then in Grafana: Explore -> Loki ->
   `{service_name="lean-shop-api"} |= "order not found"`, and jump from that log
   to its trace via the `trace_id` on the record.
5. Swap Jaeger for the OpenTelemetry Collector and add a sampler — that's the next
   real-world step, since you cannot afford 100% sampling in production.
