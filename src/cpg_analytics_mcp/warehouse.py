"""Warehouse access. DuckDB locally; same interface can front Snowflake or Databricks."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb


class QueryTimeout(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool

    def as_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]


class Warehouse:
    def __init__(self, db_path: Path, timeout_s: float):
        if not db_path.exists():
            raise FileNotFoundError(f"{db_path} not found. Run `uv run cpg-mcp-generate` first.")
        self.timeout_s = timeout_s
        self._con = duckdb.connect(
            str(db_path), read_only=True,
            config={"enable_external_access": False, "lock_configuration": True})
        self._lock = threading.Lock()

    def catalog(self) -> dict[str, dict]:
        """{table: {"type": "table"|"view", "columns": {name: type}}}."""
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
        wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT {int(max_rows) + 1}"
        with self._lock:
            cur = self._con.cursor()
            timer = threading.Timer(self.timeout_s, cur.interrupt)
            timer.start()
            try:
                rel = cur.execute(wrapped, params or [])
                columns = [d[0] for d in rel.description]
                rows = [list(r) for r in rel.fetchall()]
            except duckdb.InterruptException as e:
                raise QueryTimeout(f"Query exceeded {self.timeout_s}s timeout.") from e
            finally:
                timer.cancel()
                cur.close()
        truncated = len(rows) > max_rows
        return QueryResult(columns, rows[:max_rows], truncated)
