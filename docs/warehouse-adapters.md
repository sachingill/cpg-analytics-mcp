# Design note: warehouse adapters

**Status:** Slice 1 and contract tests implemented · **Scope:** Slice 1 (core refactor in this repo). Snowflake and Databricks adapters live in
[`mcp-warehouse-adapters`](https://github.com/sachingill/mcp-warehouse-adapters).

## Problem

Enterprises won't copy data into DuckDB. The server must run on their Snowflake or Databricks and respect their
permissions. Today the core is DuckDB-only: `App` builds the DuckDB `Warehouse` directly, `governance.py`
hard-codes `"duckdb"` as the SQL dialect, and `config.py` only knows a database file path. Adding a platform
should not require editing the governance or semantic code, because those are what make answers safe and correct.

## Decision

1. **A two-method interface.** `Warehouse` is a `typing.Protocol`:
   ```python
   class Warehouse(Protocol):
       dialect: str                      # "duckdb" | "snowflake" | "databricks"
       def catalog(self) -> dict[str, dict]: ...
       def query(self, sql: str, params: list | None = None, max_rows: int = 200) -> QueryResult: ...
   ```
   `QueryResult` and `QueryTimeout` stay in core and are shared by every adapter.
2. **Write SQL once, translate in one place.** The semantic layer keeps emitting canonical SQL (DuckDB dialect,
   `?` placeholders). One function, `to_dialect(sql, target)`, translates it with sqlglot. Adapters map `?` to
   their driver's placeholder style inside `query()`.
3. **Guardrails check in the target dialect.** `check_sql(..., dialect=wh.dialect)` parses and re-emits in the
   platform's dialect, replacing the two hard-coded `"duckdb"` strings.
4. **Adapters are plug-ins.** `CPG_MCP_WAREHOUSE=duckdb|snowflake|databricks` selects one; a factory resolves it
   through the `cpg_analytics_mcp.warehouses` entry-point group. Core never imports platform drivers.
5. **Shared vs per-platform.** Shared: result and error types, row cap (`cap_rows()`, a LIMIT n+1 wrapper valid in all three dialects),
   `to_dialect()`, catalog shape, contract tests. Per adapter: connection and auth, timeout mechanism,
   placeholder style, catalog source, query tags, read-only enforcement.
6. **Loading data is not querying.** Loaders for the synthetic data are scripts in the adapters repo, not part of
   the runtime interface.
7. **Contract tests enforce substitutability.** One parametrised suite every adapter must pass: read-only,
   `truncated` set correctly, `QueryTimeout` (never a raw driver error), same catalog shape.

## Transport: driver first, REST later

| | Driver (DB-API) | REST statement API |
|---|---|---|
| Fits | One service identity, persistent session, fast Arrow results | Per-request end-user tokens, serverless, async/long queries |
| Here | Slices 2–3 (stdio, single user) | With the HTTP + OAuth roadmap item, for identity propagation |

The choice stays inside one adapter because the rest of the server only sees `Warehouse`.

## Alternatives rejected

| Option | Why not |
|---|---|
| Abstract base-class hierarchy | No shared state to inherit; a Protocol is lighter and duck-typed |
| SQLAlchemy as the abstraction | Another layer over SQL we must control for governance; sqlglot already handles dialects |
| Hand-written SQL per platform | Duplicates every metric formula; drift breaks "one definition per KPI" |
| A shared base class for auth and timeouts | They change for different reasons per platform; the wrong abstraction costs more than duplication |
| Supporting BigQuery / Postgres now | YAGNI |

## Plan

1. **Pure refactor** (this repo): Protocol, factory, `to_dialect()`, `cap_rows()`, dialect-aware `check_sql`.
   DuckDB stays the only adapter. Behaviour unchanged; the existing tests stay green.
2. **Contract tests** (`cpg_analytics_mcp.testing.warehouse_contract`): one reusable suite, run here against DuckDB, imported by the adapters repo. Also requires lowercase column names and plain Python values (Snowflake returns UPPERCASE and `Decimal`).
3. **Snowflake adapter** (adapters repo): key-pair auth, role, query tag, statement timeout, loader, masking
   policy on `cogs_per_unit`. Integration tests run only when credentials are set.
4. **Databricks adapter**: SQL warehouse, OAuth, Unity Catalog grants and a column mask, system-table audit.

## Risks

- **Translation gaps:** a function sqlglot can't translate. Mitigation: translation tests for every metric.
- **Behaviour drift between adapters:** mitigated by the contract tests.
- **Cost on trial accounts:** smallest warehouse, auto-suspend, synthetic data only, personal accounts only.
