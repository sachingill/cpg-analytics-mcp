"""Governance: role policies, SQL guardrails, and audit logging.

Defense in depth for raw SQL coming from an LLM:
  1. parse with sqlglot (no regex) -> exactly one read-only SELECT
  2. table allowlist per role, no file-reading table functions
  3. blocked columns per role (and no SELECT * over tables with blocked columns)
  4. row cap + timeout at execution
  5. the database connection itself is read-only with external access disabled
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Copy, exp.Set, exp.Use, exp.Transaction, exp.Commit, exp.Rollback,
)
FORBIDDEN_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "sniff_csv", "query_table", "query",
}


class GuardrailError(ValueError):
    """Raised when a query violates policy. Message is safe to show to the model."""


@dataclass(frozen=True)
class RolePolicy:
    name: str
    description: str
    tables: frozenset[str] | None  # None = all
    blocked_columns: frozenset[str]
    metrics: frozenset[str] | None  # None = all

    def can_read_table(self, table: str) -> bool:
        return self.tables is None or table.lower() in self.tables

    def can_use_metric(self, metric: str) -> bool:
        return self.metrics is None or metric in self.metrics


def load_role(roles_file: Path, role: str) -> RolePolicy:
    roles = yaml.safe_load(roles_file.read_text())["roles"]
    if role not in roles:
        raise ValueError(f"Unknown role '{role}'. Known roles: {sorted(roles)}")
    r = roles[role]

    def _set(v):
        return None if v == "*" else frozenset(x.lower() for x in v)

    return RolePolicy(
        name=role,
        description=r.get("description", ""),
        tables=_set(r.get("tables", "*")),
        blocked_columns=frozenset(c.lower() for c in r.get("blocked_columns", [])),
        metrics=None if r.get("metrics", "*") == "*" else frozenset(r["metrics"]),
    )


@dataclass
class CheckedQuery:
    sql: str
    tables: list[str] = field(default_factory=list)


def check_sql(sql: str, policy: RolePolicy, table_columns: dict[str, set[str]]) -> CheckedQuery:
    """Validate an LLM-written query. Returns normalized SQL or raises GuardrailError."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise GuardrailError(f"SQL could not be parsed: {str(e).splitlines()[0]}") from e

    if len(statements) != 1:
        raise GuardrailError("Exactly one SQL statement is allowed.")
    root = statements[0]

    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise GuardrailError("Only read-only SELECT queries are allowed.")
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise GuardrailError(f"Statement type '{type(node).__name__}' is not allowed.")
        if isinstance(node, exp.Func):
            fname = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if fname in FORBIDDEN_FUNCTIONS:
                raise GuardrailError(f"Function '{fname}' is not allowed (no file or external access).")

    cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    tables = []
    for t in root.find_all(exp.Table):
        name = t.name.lower()
        if not name:  # table-valued function in FROM
            raise GuardrailError("Table functions are not allowed in FROM.")
        if name in cte_names:
            continue
        if name not in table_columns:
            raise GuardrailError(f"Unknown table '{name}'. Use list_datasets to see what exists.")
        if not policy.can_read_table(name):
            raise GuardrailError(f"Role '{policy.name}' may not read table '{name}'.")
        tables.append(name)

    if policy.blocked_columns:
        for col in root.find_all(exp.Column):
            if col.name.lower() in policy.blocked_columns:
                raise GuardrailError(
                    f"Column '{col.name}' is restricted for role '{policy.name}'. "
                    "Use a governed metric (get_metric) instead.")
        for star in root.find_all(exp.Star):
            if isinstance(star.parent, exp.Count):
                continue
            exposed = set().union(*(table_columns[t] for t in tables)) if tables else set()
            if exposed & policy.blocked_columns:
                raise GuardrailError(
                    "SELECT * is not allowed here because the table contains restricted "
                    "columns. List the columns you need explicitly.")

    return CheckedQuery(sql=root.sql(dialect="duckdb"), tables=sorted(set(tables)))


class AuditLog:
    """Append-only JSONL audit trail: who ran what, when, and what happened."""

    def __init__(self, path: Path, role: str):
        self.path = path
        self.role = role
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, tool: str, args: dict, status: str, started: float, **extra) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "role": self.role,
            "tool": tool,
            "args": args,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            **extra,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
