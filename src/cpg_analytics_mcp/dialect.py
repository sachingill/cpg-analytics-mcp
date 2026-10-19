"""SQL dialects: governed SQL is written once in a canonical dialect and translated per warehouse."""

from __future__ import annotations

import sqlglot

CANONICAL = "duckdb"  # the semantic layer and hand-written server SQL use this dialect


def to_dialect(sql: str, target: str) -> str:
    """Translate canonical SQL to the target warehouse's dialect. `?` placeholders are preserved."""
    if target == CANONICAL:
        return sql
    return sqlglot.transpile(sql, read=CANONICAL, write=target)[0]
