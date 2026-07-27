"""The arch Workbench, driven by a scripted model — for working on the page.

This is the *real* stack: `Runner`, `HttpTransport`, `ArchSession`, the real
tools, the real gates, the real background critic. Only the model is fake, so
what the browser sees is genuine replay, genuine mid-turn state pushes and
genuine threading. Verifying the page against a mocked event stream has already
cost this project twice; don't.

    python3 scripts/arch_devserver.py [port]      # default 8765

The scripted session walks: two rival sketches with an objection against the
user's own request → promote → tighten → top-level gate → depth on five facet
kinds (store, api, queue, service, llm, infra) → finalize with a blocker still
open. That covers every sub-diagram the component dialog can draw, which is the
point of the extra components.

State goes to a temp directory, printed at startup — never into the repo.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ox.engine.runner import Runner  # noqa: E402
from ox.engine.session import SessionRecorder  # noqa: E402
from ox.harnesses.arch import harness as arch_def  # noqa: E402
from ox.harnesses.arch.render import TRACKER_PREFIX  # noqa: E402
from ox.harnesses.arch.session import ArchSession  # noqa: E402
from ox.harnesses.arch.tools import arch_harness_tools  # noqa: E402
from ox.http_transport import HttpTransport  # noqa: E402
from ox.llm.registry import ModelSpec, ProviderConfig, Registry  # noqa: E402
from ox.llm.types import LLMResponse, Message, ToolCall, Usage  # noqa: E402
from ox.repl import Repl  # noqa: E402
from ox.serve import Server  # noqa: E402
from ox.tools import ToolContext  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
RUN = Path(tempfile.gettempdir()) / "ox-arch-devrun"
# per model call — the six expansions land one after another, and a page whose
# mid-turn behaviour you can't watch is a page you can't check
PAUSE = 0.9

SPEC = ModelSpec(
    spec="fake:architect",
    provider=ProviderConfig(name="fake", base_url="http://x"),
    model="architect",
    context_window=200000,
)
_ids = iter(range(10000))


def tc(name, args):
    return ToolCall(id=f"c{next(_ids)}", name=name, arguments=args, arguments_json=json.dumps(args))


def msg(content=None, calls=()):
    return Message(role="assistant", content=content, tool_calls=list(calls))


class SlowFakeClient:
    """Streams content a word at a time, so the page's live text is real."""

    def __init__(self, script):
        self.script = list(script)

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
        m = self.script.pop(0) if self.script else msg("(script exhausted — say something else)")
        if on_delta is not None and m.content:
            for word in m.content.split(" "):
                on_delta(word + " ")
                time.sleep(0.035)
            on_delta(None)
        time.sleep(PAUSE)
        return LLMResponse(message=m, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


SCRIPT = [
    # ---- turn A: sketch two rival shapes, push back on the request ----
    msg(calls=[
        tc("variant", {"id": "v1", "name": "synchronous", "summary": "checkout calls fulfilment inline"}),
        tc("node", {"id": "storefront", "label": "storefront-web", "kind": "ui", "note": "catalogue and checkout"}),
        tc("node", {"id": "order-svc", "label": "order-service", "kind": "service", "note": "owns the order lifecycle"}),
        tc("node", {"id": "order-db", "label": "order-db", "kind": "store", "note": "orders, lines, intents"}),
        tc("node", {"id": "carrier", "label": "carrier-api", "kind": "external", "note": "books the shipment"}),
        tc("link", {"src": "storefront", "dst": "order-svc", "label": "POST /orders"}),
        tc("link", {"src": "order-svc", "dst": "order-db", "label": "writes", "kind": "sync"}),
        tc("link", {"src": "order-svc", "dst": "carrier", "label": "book", "kind": "sync"}),
    ]),
    msg(calls=[
        tc("variant", {"id": "v2", "name": "evented", "summary": "fulfilment hangs off a bus"}),
        tc("node", {"id": "storefront", "label": "storefront-web", "kind": "ui"}),
        tc("node", {"id": "order-api", "label": "order-api", "kind": "api", "note": "public order surface"}),
        tc("node", {"id": "order-svc", "label": "order-service", "kind": "service"}),
        tc("node", {"id": "order-db", "label": "order-db", "kind": "store"}),
        tc("node", {"id": "bus", "label": "events-bus", "kind": "queue", "note": "nats · at-least-once"}),
        tc("node", {"id": "worker", "label": "fulfilment-worker", "kind": "job"}),
        tc("link", {"src": "storefront", "dst": "order-api", "label": "POST /orders"}),
        tc("link", {"src": "order-api", "dst": "order-svc", "label": "calls"}),
        tc("link", {"src": "order-svc", "dst": "order-db", "label": "writes"}),
        tc("link", {"src": "order-svc", "dst": "bus", "label": "OrderPlaced", "kind": "async"}),
        tc("link", {"src": "bus", "dst": "worker", "label": "consume", "kind": "async"}),
    ]),
    msg(
        content=(
            "Two takes. The synchronous one is fewer moving parts, but checkout waits on the "
            "carrier — a carrier outage becomes a checkout outage. The evented one costs you a "
            "bus to run and eventual consistency on fulfilment status.\n\n"
            "I'd take the evented shape. Which way do you want to go?"
        ),
        calls=[tc("concern", {
            "severity": "blocker", "target": "user",
            "claim": "calling the carrier inline means a carrier outage takes checkout down with it",
            "alternative": "put fulfilment behind the bus and let checkout return as soon as the order is durable",
        })],
    ),
    msg(content="(waiting on you — sketch layer is live)"),
    # ---- turn B: promote the evented shape and tighten it ----
    msg(
        content="Going with the evented shape — promoting it and tightening it up.",
        calls=[
            tc("promote", {"variant_id": "v2"}),
            tc("component", {"id": "order-svc", "trace": ["place an order"],
                             "responsibility": "owns the order lifecycle"}),
            tc("component", {"id": "order-db", "kind": "store", "data_owned": "orders, lines, payment intents",
                             "trace": ["place an order"], "responsibility": "durable order state"}),
            tc("component", {"id": "pricing", "kind": "llm", "responsibility": "explains price changes to shoppers",
                             "trace": ["place an order"], "tech": "small model, cached"}),
            tc("component", {"id": "cluster", "kind": "infra", "responsibility": "where all this runs",
                             "trace": ["place an order"]}),
            tc("connect", {"src": "order-svc", "dst": "bus", "label": "OrderPlaced", "kind": "async",
                           "mechanism": "nats jetstream"}),
            tc("connect", {"src": "order-api", "dst": "pricing", "label": "explain", "kind": "sync"}),
            tc("flow", {"id": "place-order", "name": "place order", "kind": "happy", "steps": [
                {"src": "storefront", "dst": "order-api", "action": "POST /orders"},
                {"src": "order-api", "dst": "order-svc", "action": "create order"},
                {"src": "order-svc", "dst": "order-db", "action": "INSERT order"},
                {"src": "order-svc", "dst": "bus", "action": "publish OrderPlaced"},
                {"src": "bus", "dst": "worker", "action": "consume + book carrier"},
            ]}),
            tc("decide", {"topic": "Event transport", "category": "communication",
                          "options": [{"name": "NATS", "pros": ["already in-cluster"]},
                                      {"name": "Kafka", "cons": ["ordering guarantees we don't need"]}],
                          "choice": "NATS", "rationale": "already run in-cluster; volume is low"}),
            tc("concern", {"resolve": "c1", "status": "accepted",
                           "resolution": "fulfilment moved behind the bus, so checkout no longer waits on the carrier"}),
        ],
    ),
    msg(content="Promoted and tightened. Say the word and I'll take it to approval."),
    msg(content="Top level's ready — take a look.",
        calls=[tc("done", {"summary": "evented shape, 8 components, one happy flow"})]),
    # ---- turn C: depth, one facet kind at a time ----
    msg(calls=[tc("expand", {
        "component_id": "order-db",
        "entities": [
            {"name": "order", "keys": "id",
             "fields": ["id", "user_id", "status", "total_cents", "placed_at"],
             "indexes": ["user_id, placed_at desc"]},
            {"name": "order_line", "keys": "id",
             "fields": ["id", "order_id", "sku", "qty", "unit_cents"]},
            {"name": "user", "keys": "id", "fields": ["id", "email"]},
            {"name": "outbox", "keys": "id", "fields": ["id", "event_type", "payload", "published_at"]},
        ],
        "access_patterns": ["orders by user, newest first", "unpublished outbox rows, oldest first"],
        "migration_risk": "order_line is written on every checkout — an online migration needs a backfill window",
    })]),
    msg(calls=[tc("expand", {
        "component_id": "order-api",
        "endpoints": [
            {"route": "/orders", "method": "POST", "request": "{items[], address}", "response": "{id, status}",
             "auth": "session cookie", "errors": ["422 invalid address", "409 duplicate idempotency key"],
             "idempotency": "Idempotency-Key header, 24h window"},
            {"route": "/orders", "method": "GET", "request": "?cursor&limit", "response": "{orders[], next}",
             "auth": "session cookie", "errors": ["401"], "pagination": "cursor on placed_at"},
            {"route": "/orders/{id}", "method": "GET", "request": "—", "response": "{order, lines[]}",
             "auth": "session cookie", "errors": ["404", "403 not yours"]},
        ],
    })]),
    msg(calls=[tc("expand", {
        "component_id": "bus",
        "messages": [
            {"name": "OrderPlaced", "schema": "{order_id, user_id, total_cents, placed_at}",
             "ordering": "per order_id", "delivery": "at-least-once",
             "dlq_policy": "5 attempts then orders.dlq, alert on depth > 0"},
            {"name": "ShipmentBooked", "schema": "{order_id, carrier_ref}",
             "ordering": "none", "delivery": "at-least-once"},
        ],
    })]),
    msg(calls=[tc("expand", {
        "component_id": "order-svc",
        "interface": ["createOrder(cart, address) -> Order", "cancelOrder(id, reason)"],
        "modules": [
            {"name": "intake", "purpose": "validates the cart and the address before anything is written"},
            {"name": "pricing", "purpose": "recomputes totals server-side; never trusts the client"},
            {"name": "persistence", "purpose": "writes the order and its outbox row in one transaction"},
        ],
    })]),
    msg(calls=[tc("expand", {
        "component_id": "pricing",
        "tasks": [{
            "name": "explain-price-change", "model_tier": "small",
            "prompt_contract": "cart + previous total + current total -> one sentence for a shopper",
            "context_strategy": "the two totals and the changed lines only; never the whole catalogue",
            "guardrails": "no promises about future prices; refuse if the delta is zero",
            "fallback": "show the itemised diff with no prose",
            "eval_hook": "50 golden carts, weekly",
            "cost_envelope": "< $0.001 per checkout",
        }],
    })]),
    msg(calls=[tc("expand", {
        "component_id": "cluster",
        "units": [
            {"name": "edge", "components": ["storefront", "order-api"],
             "scaling_policy": "2-10 pods on p95 latency", "region": "eu-west-1"},
            {"name": "core", "components": ["order-svc", "worker", "pricing"],
             "scaling_policy": "3 pods, manual", "region": "eu-west-1"},
            {"name": "data", "components": ["order-db", "bus"],
             "scaling_policy": "vertical only", "region": "eu-west-1"},
        ],
        "state_locality": "all state in eu-west-1; the edge is stateless and can move",
    })]),
    msg(
        content="Depth is in on everything that would hurt to get wrong. One thing I still don't like.",
        calls=[tc("concern", {
            "severity": "blocker", "target": "order-db",
            "claim": "no retention policy on the outbox — it grows forever and nothing prunes it",
            "alternative": "publish-then-delete, or a nightly prune of published rows",
        })],
    ),
    msg(content="Ready to finalize when you are.",
        calls=[tc("done", {"summary": "evented order flow, one open objection"})]),
]


def main() -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    recorder = SessionRecorder(RUN)
    registry = Registry(providers={}, models={},
                        aliases={"default": "fake:architect", "judge": "fake:architect"})
    ctx = ToolContext(repo_root=Path.cwd(), record=recorder.event)
    runner = Runner(
        spec=SPEC, client=SlowFakeClient(SCRIPT), registry=registry,
        tools=arch_harness_tools(with_kg=False, with_web=False), ctx=ctx,
        instructions_path=arch_def.INSTRUCTIONS_PATH,
        mutating_tools=arch_def.MUTATING_TOOLS,
        tracker=arch_def.arch_tracker,
        tracker_prefix=TRACKER_PREFIX,
        explore_nudge=arch_def.EXPLORE_NUDGE,
    )
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="arch-dev")
    transport = HttpTransport(static_dir=arch_def.STATIC_DIR, port=PORT)
    server = Server(repl, transport=transport)
    arch = ArchSession(run_dir=RUN, broker=server.broker, on_state=transport.emit)
    # a deterministic "critic", so the background pass is exercised too
    arch.judge = lambda state: (
        [{"severity": "risk", "target": "bus",
          "claim": "no dead-letter path — a poisoned message blocks the consumer forever",
          "alternative": "a DLQ subject with an alert on depth"}]
        if "bus" in state.components else []
    )
    ctx.arch = arch

    threading.Thread(target=server.run, daemon=True).start()
    print(f"arch dev server: http://127.0.0.1:{PORT}/   (state in {RUN})", flush=True)
    time.sleep(0.4)
    server.on_user_input("design order capture and fulfilment for our storefront")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
