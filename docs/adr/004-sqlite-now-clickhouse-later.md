# ADR 004 — SQLite for the take-home, ClickHouse-shaped schema

**Decision.** SQLite via SQLAlchemy 2.0 for the prototype; the schema is what we'd use on ClickHouse in production (one row per artifact, foreign keys to the shipment, large JSON blobs for the agent outputs).

**Why SQLite for now.** Zero deps. The reviewer doesn't need to start a database. `make demo` works on a fresh clone.

**Why this shape will port to ClickHouse.** Append-only tables, denormalised JSON for agent outputs, integer counts pre-aggregated on the validator row (`n_match`, `n_mismatch`, `n_uncertain`) for cheap dashboard queries, and `created_at` indexed on every table. The NL→SQL agent's few-shot examples are written in SQLite dialect but translate trivially to ClickHouse.

**What we are NOT doing.** No ORM relations in the hot path — repository methods write rows directly. No migrations framework. No connection pool tuning. All of those belong in the production path, not the prototype.
