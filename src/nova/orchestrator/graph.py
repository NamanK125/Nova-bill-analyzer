"""LangGraph wiring — Nova's 5-stage pipeline.

scope_resolution → context_compilation → schema_routing
                 → extractor → validator → router
                 → evidence_delivery → END

The three agent nodes are conceptually one "plan+execute" stage but are
modelled as separate nodes so each one is independently checkpointed,
re-runnable, and observable.

Persistence: an `AsyncSqliteSaver` (aiosqlite-backed) writes per-node state to
`settings.checkpoint_db` so the graph can resume from the last successful node
after a crash. The thread is keyed by `shipment_id`; pass it via
`config={"configurable": {"thread_id": shipment_id}}` on invoke.

Call shape:
    async with graph_session() as graph:
        await graph.ainvoke(initial, config={"configurable": {"thread_id": sid}})

`graph_session()` enters the AsyncSqliteSaver context and yields a compiled
graph that uses it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph

from nova.config import get_settings
from nova.orchestrator.nodes import (
    context_compilation,
    evidence_delivery,
    plan_execute_extract,
    plan_execute_route,
    plan_execute_validate,
    schema_routing,
    scope_resolution,
)
from nova.orchestrator.state import GraphState


def _build_uncompiled() -> StateGraph:
    g = StateGraph(GraphState)

    g.add_node("scope_resolution", scope_resolution)
    g.add_node("context_compilation", context_compilation)
    g.add_node("schema_routing", schema_routing)
    g.add_node("extractor", plan_execute_extract)
    g.add_node("validator", plan_execute_validate)
    g.add_node("router", plan_execute_route)
    g.add_node("evidence_delivery", evidence_delivery)

    g.set_entry_point("scope_resolution")
    g.add_edge("scope_resolution", "context_compilation")
    g.add_edge("context_compilation", "schema_routing")
    g.add_edge("schema_routing", "extractor")
    g.add_edge("extractor", "validator")
    g.add_edge("validator", "router")
    g.add_edge("router", "evidence_delivery")
    g.add_edge("evidence_delivery", END)

    return g


@asynccontextmanager
async def graph_session() -> AsyncIterator[Any]:
    """Yield a compiled graph backed by an AsyncSqliteSaver.

    The checkpointer is entered as an async context manager so it cleans up its
    aiosqlite connection on exit. Re-entering this for each invoke is cheap
    (build is dict-merges; saver opens one sqlite connection).
    """
    s = get_settings()
    db_path = Path(s.checkpoint_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        compiled = _build_uncompiled().compile(checkpointer=saver)
        yield compiled


def build_graph(*, checkpointer: Any | None = None) -> Any:
    """Stateless build — for tests and ad-hoc usage. Does NOT attach the
    checkpointer by default. Prefer `graph_session()` for the real path."""
    return _build_uncompiled().compile(checkpointer=checkpointer)
