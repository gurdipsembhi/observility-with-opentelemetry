# Running the backends

The app emits OTLP. Anything that speaks OTLP can receive it, so "run Grafana"
and "run OpenTelemetry" are the same two-step move:

1. start something that listens on **localhost:4317**
2. start the app with **`OTEL_OTLP=1`**

Nothing in the Python code changes between the options below.

---

## 0. Start Docker first

The daemon isn't running right now. On macOS:

```bash
open -a Docker          # then wait for the whale icon to settle
docker info             # works = daemon is up
```

---

## Option A — Grafana (recommended)

`grafana/otel-lgtm` is Grafana's own teaching image: an OTel Collector, **T**empo
(traces), **P**rometheus (metrics), **L**oki (logs) and Grafana, already wired
together. One container, no configuration.

```bash
docker compose up -d              # first run pulls ~1.5 GB
docker compose logs -f lgtm       # wait for "The OpenTelemetry collector and the Grafana LGTM stack are up and running"
```

Then run the app against it, and generate some traffic:

```bash
OTEL_OTLP=1 .venv/bin/uvicorn main:app --port 8000     # terminal 1
./load.sh                                              # terminal 2
```

Open **http://localhost:3000** — no login required.

### What to click, in order

**Traces.** Explore → data source **Tempo** → *Search* tab → Service Name
`lean-shop-api` → Run query. Click any `GET /orders/{order_id}` row.

You get the waterfall: `process_order` on top, `db.query` and `price_order`
beneath it, and the httpx client span with the downstream
`GET /internal/inventory/{sku}` server span nested *inside* it. Click a span to
see its attributes (`order.id`, `db.statement`) and the `order.priced` event.

Then switch to the *TraceQL* tab and try:

```
{ resource.service.name = "lean-shop-api" }
{ name = "process_order" && duration > 50ms }
{ status = error }
{ span.order.total > 20 }
```

That last one is the payoff: you are querying your own business attribute across
every request. This is what people mean by "high-cardinality" observability —
you couldn't do it with a metrics dashboard.

**Metrics.** Explore → data source **Prometheus** → Metrics browser → type
`orders`. You'll find `orders_processed_total` and the `orders_value_*` histogram
buckets. Note the renaming: OTel's `orders.processed` becomes Prometheus's
`orders_processed_total` — dots to underscores, counters get `_total`. Try:

```promql
sum by (outcome) (rate(orders_processed_total[1m]))
histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_duration_milliseconds_bucket[5m])))
```

The second one is your p95 latency per route, built entirely from spans the
auto-instrumentation produced.

**Logs.** Explore -> Loki -> `{service_name="lean-shop-api"}`. The app's own
lines and uvicorn's access log both land here, and each record carries the
`trace_id` and `span_id` it was written inside — that's the jump from a log to
its trace. `{service_name="lean-shop-api"} |= "order not found"` finds the 404s.

The same lines still print to your terminal; OTLP is an extra destination, not a
replacement.

Stop it with `docker compose down` (add `-v` to wipe the stored data).

---

## Option B — Jaeger (traces only, simpler UI)

Nothing to learn about dashboards; just a clean trace viewer.

```bash
docker compose --profile jaeger up -d jaeger
OTEL_OTLP=1 .venv/bin/uvicorn main:app --port 8000
```

**http://localhost:16686** → Service `lean-shop-api` → Find Traces.

Only start one of A and B at a time — both bind port 4317.

---

## Option C — a real OTel Collector in front of Grafana

Options A and B send app → backend directly. In production you almost always put
a Collector in between, so apps stay dumb and routing/filtering/sampling is
config you can change without redeploying anything.

```bash
docker compose --profile collector up -d              # starts lgtm + otelcol
OTEL_OTLP=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319 \
  .venv/bin/uvicorn main:app --port 8000
docker compose logs -f otelcol                        # watch spans arrive
```

The pipeline lives in [otel-collector-config.yaml](otel-collector-config.yaml)
and is worth reading — it's only three ideas:

- **receivers** — how data gets in (OTLP on 4317/4318)
- **processors** — what happens to it in flight (`batch`, `resource` to stamp an
  attribute, `filter/drop_health` to throw away health-check spans)
- **exporters** — where it goes (`debug` prints to the container log, `otlp/lgtm`
  forwards to Grafana)

...tied together in **pipelines**, one per signal.

Prove it works: hit `/health` a few times, then search Tempo for it. Those spans
are gone — dropped by the filter processor, never stored. That single config
block is a large part of why the Collector exists.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop isn't running — `open -a Docker` |
| `port is already allocated` on 4317 | Jaeger and lgtm are both up. `docker compose down` and start one |
| Grafana shows nothing | App not started with `OTEL_OTLP=1`; or check the time range is *Last 15 minutes*; spans batch for up to 5s before export |
| Spans print to the terminal instead | That's console mode — expected without `OTEL_OTLP=1` |
| Want both at once | `OTEL_OTLP=1 OTEL_CONSOLE=1` sends to the backend *and* prints |
