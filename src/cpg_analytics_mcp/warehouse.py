"""Warehouse access behind a small interface. DuckDB is built in; Snowflake and Databricks are plug-ins.

Adapters register a factory `(Settings) -> Warehouse` under the `cpg_analytics_mcp.warehouses`
entry-point group, so core never imports platform drivers.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import duckdb

if TYPE_CHECKING:
    from cpg_analytics_mcp.config import Settings

ENTRY_POINT_GROUP = "cpg_analytics_mcp.warehouses"


class QueryTimeout(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool

    def as_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=True)) for r in self.rows]


class Warehouse(Protocol):
    """What the server needs from any warehouse. Every adapter must also be read-only,
    set `truncated` when rows exceed `max_rows`, and raise QueryTimeout (never a driver error)."""

    dialect: str

    def catalog(self) -> dict[str, dict]:
        """{table: {"type": "table"|"view", "columns": {name: type}}}."""
        ...

    def query(self, sql: str, params: list | None = None, max_rows: int = 200) -> QueryResult:
        """Run SQL in this warehouse's dialect, with `?` placeholders bound to `params`."""
        ...


def cap_rows(sql: str, max_rows: int) -> str:
    """Wrap a query so it returns at most max_rows + 1 rows (the extra row signals truncation)."""
    return f"SELECT * FROM ({sql}) AS _q LIMIT {int(max_rows) + 1}"


class DuckDBWarehouse:
    dialect = "duckdb"

    def __init__(self, db_path: Path, timeout_s: float):
        if not db_path.exists():
            raise FileNotFoundError(f"{db_path} not found. Run `uv run cpg-mcp-generate` first.")
        self.timeout_s = timeout_s
        self._con = duckdb.connect(
            str(db_path), read_only=True,
            config={"enable_external_access": False, "lock_configuration": True})
        self._lock = threading.Lock()

    def catalog(self) -> dict[str, dict]:
        rows = self._con.execute("""
            SELECT c.table_name, t.table_type, c.column_name, c.data_type
            FROM information_schema.columns c
            JOIN information_schema.tables t USING (table_schema, table_name)
            WHERE c.table_schema = 'main'
            ORDER BY c.table_name, c.ordinal_position
        """).fetchall()
        out: dict[str, dict] = {}
        for table, ttype, col, dtype in rows:
            entry = out.setdefault(table, {"type": "view" if ttype == "VIEW" else "table", "columns": {}})
            entry["columns"][col] = dtype
        return out

    def query(self, sql: str, params: list | None = None, max_rows: int = 200) -> QueryResult:
        with self._lock:
            cur = self._con.cursor()
            timer = threading.Timer(self.timeout_s, cur.interrupt)
            timer.start()
            try:
                rel = cur.execute(cap_rows(sql, max_rows), params or [])
                columns = [d[0] for d in rel.description]
                rows = [list(r) for r in rel.fetchall()]
            except duckdb.InterruptException as e:
                raise QueryTimeout(f"Query exceeded {self.timeout_s}s timeout.") from e
            finally:
                timer.cancel()
                cur.close()
        truncated = len(rows) > max_rows
        return QueryResult(columns, rows[:max_rows], truncated)


def _duckdb_factory(settings: Settings) -> Warehouse:
    return DuckDBWarehouse(settings.db_path, settings.query_timeout_s)


BUILTIN: dict[str, Callable[[Settings], Warehouse]] = {"duckdb": _duckdb_factory}


def load_warehouse(settings: Settings) -> Warehouse:
    """Build the warehouse named by settings.warehouse: built-in, or a registered plug-in."""
    name = settings.warehouse
    if name in BUILTIN:
        return BUILTIN[name](settings)
    plugins = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}
    if name not in plugins:
        available = sorted({*BUILTIN, *plugins})
        raise ValueError(f"Unknown warehouse '{name}'. Available: {available}. "
                         "Install mcp-warehouse-adapters for Snowflake or Databricks.")
    return plugins[name].load()(settings)
