"""Semantic layer: turns (metrics, dimensions, filters) into safe, governed SQL."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml


class MetricError(ValueError):
    pass


@dataclass(frozen=True)
class Metric:
    name: str
    source: str
    expr: str
    description: str
    derived_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class Source:
    name: str
    table: str
    time_column: str
    description: str
    dimensions: tuple[str, ...]


class SemanticLayer:
    def __init__(self, metrics_file: Path):
        spec = yaml.safe_load(metrics_file.read_text())
        self.sources = {
            k: Source(k, v["table"], v["time_column"], v["description"], tuple(v["dimensions"]))
            for k, v in spec["sources"].items()
        }
        self.metrics = {
            k: Metric(k, v["source"], v["expr"], " ".join(v["description"].split()),
                      tuple(c.lower() for c in v.get("derived_from", [])))
            for k, v in spec["metrics"].items()
        }

    def covering_metrics(self) -> dict[str, list[str]]:
        """{restricted column: [metrics computed from it]}, from `derived_from`."""
        out: dict[str, list[str]] = {}
        for m in self.metrics.values():
            for col in m.derived_from:
                out.setdefault(col, []).append(m.name)
        return {c: sorted(ms) for c, ms in out.items()}

    def build(
        self,
        metrics: list[str],
        group_by: list[str] | None = None,
        filters: dict[str, str | list[str]] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> tuple[str, list]:
        if not metrics:
            raise MetricError("Provide at least one metric.")
        unknown = [m for m in metrics if m not in self.metrics]
        if unknown:
            raise MetricError(f"Unknown metric(s) {unknown}. Use list_metrics.")
        src_names = {self.metrics[m].source for m in metrics}
        if len(src_names) > 1:
            raise MetricError(f"Metrics come from different sources {sorted(src_names)}; query them separately.")
        src = self.sources[src_names.pop()]

        group_by = group_by or []
        filters = filters or {}
        # Identifiers come only from the allowlist; values are always bound parameters.
        bad = [d for d in [*group_by, *filters] if d not in src.dimensions]
        if bad:
            raise MetricError(f"Dimension(s) {bad} not available for '{src.name}'. Allowed: {list(src.dimensions)}")

        where, params = [], []
        for dim, val in filters.items():
            vals = val if isinstance(val, list) else [val]
            where.append(f"CAST({dim} AS VARCHAR) IN ({', '.join('?' * len(vals))})")
            params.extend(str(v) for v in vals)
        for bound, op in ((start_date, ">="), (end_date, "<=")):
            if bound:
                try:
                    dt.date.fromisoformat(bound)
                except ValueError as e:
                    raise MetricError(f"Dates must be YYYY-MM-DD, got '{bound}'.") from e
                where.append(f"{src.time_column} {op} CAST(? AS DATE)")
                params.append(bound)

        select = [*group_by, *(f"{self.metrics[m].expr} AS {m}" for m in metrics)]
        sql = f"SELECT {', '.join(select)} FROM {src.table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if group_by:
            cols = ", ".join(group_by)
            sql += f" GROUP BY {cols} ORDER BY {cols}"
        return sql, params
