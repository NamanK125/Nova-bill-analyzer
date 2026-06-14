# ADR 001 — LangGraph for orchestration

**Decision.** LangGraph with the `SqliteSaver` checkpointer drives the 5-stage pipeline.

**Considered.** Temporal (durable-execution heavyweight); Prefect/Dagster (data-pipeline DSLs); pure asyncio (no checkpointing); LangChain `Runnable`s (too many abstractions for a typed pipeline).

**Why LangGraph.** It is Nova's actual stack. Native per-node checkpointing solves crash recovery without a separate workflow engine. Graph-as-code lets the same definition run as a take-home prototype and (in production) behind Nova's React Flow visual editor — both serialise the same graph. The cost is one Python framework dependency, which is acceptable.

**What we are NOT doing.** We do not wrap LLM calls in LangChain `Runnable`s. Each agent is a single async function with a Pydantic input contract and a Pydantic output contract; the orchestrator is responsible for the DAG, not for the prompt template plumbing.
