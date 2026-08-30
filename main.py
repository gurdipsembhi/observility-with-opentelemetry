"""A tiny "shop" API, wired for observability.

One request to /orders/{id} produces a trace like:

  GET /orders/{order_id}          (auto: FastAPI server span)
   |- validate_order              (manual span)
   |- db.query orders             (manual span, simulated DB call)
   |- GET /internal/inventory/{sku}   (auto: httpx client span)
       `- GET /internal/inventory/{sku}   (auto: server span, SAME trace)
   `- price_order                 (manual span)
"""

import asyncio
import logging
import random

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry.trace import Status, StatusCode

from telemetry import meter, setup_telemetry, tracer

# Must run before the instrumentors below hook anything up.
setup_telemetry()

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402

app = FastAPI(title="lean-shop-api")

# Auto-instrumentation: server spans for every route, client spans for every
# httpx call, with trace context propagated between them via HTTP headers.
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

# --- Logging -----------------------------------------------------------------

# A plain stdlib logger. telemetry.py has already pointed the root logger at
# stdout and (with OTEL_OTLP=1) at the backend, so nothing here is OTel-aware.
log = logging.getLogger("shop")

# --- Metrics -----------------------------------------------------------------

orders_total = meter.create_counter(
    "orders.processed",
    unit="1",
    description="Orders processed, by outcome",
)
order_value = meter.create_histogram(
    "orders.value",
    unit="USD",
    description="Order value distribution",
)

# --- Fake data ---------------------------------------------------------------

CATALOG = {
    1: {"sku": "COFFEE-01", "name": "Ethiopian beans", "price": 18.50},
    2: {"sku": "MUG-07", "name": "Enamel mug", "price": 12.00},
    3: {"sku": "GRINDER-3", "name": "Hand grinder", "price": 74.00},
}

BASE_URL = "http://127.0.0.1:8000"


@app.get("/")
async def root():
    return {
        "try": [
            "/orders/1",
            "/orders/3",
            "/external/ip",
            "/boom",
            "/health",
        ]
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    """The interesting one: nested manual spans + a real HTTP hop."""
    with tracer.start_as_current_span("process_order") as span:
        # Attributes are the searchable dimensions of a span. Put the things
        # you'd want to filter by in an incident here.
        span.set_attribute("order.id", order_id)

        item = await _load_order(order_id)
        if item is None:
            span.set_status(Status(StatusCode.ERROR, "order not found"))
            orders_total.add(1, {"outcome": "not_found"})
            # `extra` fields become log attributes on the exported record, so
            # you can filter on them in the backend.
            log.warning("order not found", extra={"order.id": order_id})
            raise HTTPException(status_code=404, detail="order not found")

        span.set_attribute("order.sku", item["sku"])
        stock = await _check_inventory(item["sku"])

        total = await _price_order(item)
        span.set_attribute("order.total", total)

        # Events are timestamped logs attached to the span.
        span.add_event("order.priced", {"total": total, "in_stock": stock["in_stock"]})

        log.info(
            "order priced",
            extra={"order.id": order_id, "sku": item["sku"], "total": total},
        )

        orders_total.add(1, {"outcome": "ok"})
        order_value.record(total, {"sku": item["sku"]})

        return {"order_id": order_id, "item": item, "total": total, **stock}

async def _load_order(order_id: int):
    with tracer.start_as_current_span("db.query") as span:
        # Semantic conventions: standard attribute names backends understand.
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.statement", "SELECT * FROM orders WHERE id = $1")
        await asyncio.sleep(random.uniform(0.005, 0.04))  # simulated query time
        return CATALOG.get(order_id)


async def _check_inventory(sku: str):
    """Calls back into this same app over HTTP, so you can see context propagate."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        resp = await client.get(f"/internal/inventory/{sku}")
        resp.raise_for_status()
        return resp.json()


async def _price_order(item):
    with tracer.start_as_current_span("price_order") as span:
        await asyncio.sleep(random.uniform(0.002, 0.01))
        total = round(item["price"] * 1.2, 2)  # tax
        span.set_attribute("pricing.tax_rate", 0.2)
        return total


@app.get("/internal/inventory/{sku}")
async def inventory(sku: str):
    """Stands in for a downstream service."""
    with tracer.start_as_current_span("inventory.lookup") as span:
        span.set_attribute("inventory.sku", sku)
        await asyncio.sleep(random.uniform(0.01, 0.06))
        count = random.randint(0, 25)
        span.set_attribute("inventory.count", count)

        log.info("inventory checked", extra={"sku": sku, "count": count})

        return {"sku": sku, "in_stock": count > 0, "count": count}


@app.get("/external/ip")
async def external_ip():
    """A real outbound call, so you get a client span to a third party."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get("https://api.github.com/zen")
        return {"status": resp.status_code, "body": resp.text.strip()}


@app.get("/boom")
async def boom():
    """Unhandled error -> span with ERROR status and a recorded exception."""
    with tracer.start_as_current_span("risky_work"):
        try:
            raise RuntimeError("payment gateway exploded")
        except RuntimeError:
            # log.exception() records the traceback; re-raising still leaves
            # the span with an ERROR status and a recorded exception.
            log.exception("order failed")
            raise
