"""CPG Analytics MCP server.

Gives an AI assistant governed, read-only access to CPG commercial data:
sell-out, promotions (post-event analysis / ROI), and sell-in vs target.
"""

from __future__ import annotations

import logging
import sys
import time
from functools import cached_property
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cpg_analytics_mcp.config import PACKAGE_DIR, Settings
from cpg_analytics_mcp.governance import AuditLog, GuardrailError, check_sql, load_role
from cpg_analytics_mcp.semantic import MetricError, SemanticLayer
from cpg_analytics_mcp.warehouse import QueryTimeout, Warehouse

# stdio transport owns stdout; all logs go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cpg-mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

TABLE_DOCS = {
    "dim_week": "Calendar of week-ending dates (Saturday) with fiscal year/month/quarter.",
    "dim_product": "SKU master: brand, category, PPG (promoted product group), pack, list price, unit cost.",
    "dim_retailer": "Retail customers and their channel (Grocery, Discounter, Convenience, Pharmacy, Online).",
    "fact_sales": "Weekly sell-out (EPOS) by SKU x retailer, with modelled baseline and promo link.",
    "fact_promotions": "Promotion calendar: mechanic, discount depth, feature/display, dates, fixed cost.",
    "fact_shipments": "Weekly sell-in: units ordered by and shipped to each retailer.",
    "fact_targets": "Monthly shipment value targets by retailer x brand.",
    "v_sales_enriched": "fact_sales joined to all dimensions and promo attributes. Start here for sales questions.",
    "v_promo_performance": "One row per promotion with uplift, post-promo dip, trade spend and ROI.",
    "v_ship_attainment": "Monthly shipped value vs target and case fill rate by retailer x brand.",
}

INSTRUCTIONS = """\
Governed, read-only CPG commercial analytics (fictional health & hygiene brands).
Prefer get_metric for any standard KPI (sales, uplift, trade spend, promo ROI, ship attainment):
it uses the single approved definition. Use run_sql only for ad-hoc questions the metrics
cannot answer. Read the cpg://glossary resource if a business term is unclear.
Always state the time window and filters behind any number you report.
"""


class App:
    """Holds per-process state; built lazily so tests can inject settings."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.policy = load_role(settings.roles_file, settings.role)
        self.audit = AuditLog(settings.audit_log, settings.role)
        self.semantic = SemanticLayer(settings.metrics_file)

    @cached_property
    def wh(self) -> Warehouse:
        return Warehouse(self.settings.db_path, self.settings.query_timeout_s)

    @cached_property
    def catalog(self) -> dict[str, dict]:
        return self.wh.catalog()

    @cached_property
    def table_columns(self) -> dict[str, set[str]]:
        return {t: {c.lower() for c in v["columns"]} for t, v in self.catalog.items()}

    def run(self, tool: str, args: dict, fn):
        """Execute a tool body with uniform error handling and auditing."""
        started = time.perf_counter()
        try:
            result = fn()
        except (GuardrailError, MetricError) as e:
            self.audit.write(tool, args, "rejected", started, error=str(e))
            raise ToolError(str(e)) from e
        except QueryTimeout as e:
            self.audit.write(tool, args, "timeout", started, error=str(e))
            raise ToolError(str(e)) from e
        except ToolError as e:
            self.audit.write(tool, args, "error", started, error=str(e))
            raise
        except Exception as e:  # never leak internals to the model
            log.exception("tool %s failed", tool)
            self.audit.write(tool, args, "error", started, error=type(e).__name__)
            raise ToolError(f"Internal error ({type(e).__name__}). The failure was logged.") from e
        rows = result.get("row_count") if isinstance(result, dict) else None
        self.audit.write(tool, args, "ok", started, rows=rows)
        return result


def create_server(settings: Settings | None = None) -> MCPServer:
    app = App(settings or Settings.from_env())
    mcp = MCPServer(name="cpg-analytics", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(annotations=READ_ONLY)
    def list_datasets() -> dict[str, Any]:
        """List the tables and views your role can query, with a one-line description of each."""
        def body():
            items = [
                {"name": t, "type": v["type"], "description": TABLE_DOCS.get(t, "")}
                for t, v in app.catalog.items() if app.policy.can_read_table(t)
            ]
            return {"role": app.policy.name, "datasets": items, "row_count": len(items)}
        return app.run("list_datasets", {}, body)

    @mcp.tool(annotations=READ_ONLY)
    def describe_table(table: str) -> dict[str, Any]:
        """Show columns and types for a table or view. Restricted columns for your role are hidden."""
        def body():
            t = table.lower()
            if t not in app.catalog or not app.policy.can_read_table(t):
                raise ToolError(f"Table '{table}' not found or not accessible. Use list_datasets.")
            cols = [
                {"name": c, "type": ty}
                for c, ty in app.catalog[t]["columns"].items()
                if c.lower() not in app.policy.blocked_columns
            ]
            return {"table": t, "description": TABLE_DOCS.get(t, ""), "columns": cols, "row_count": len(cols)}
        return app.run("describe_table", {"table": table}, body)

    @mcp.tool(annotations=READ_ONLY)
    def run_sql(sql: str, max_rows: int = 100) -> dict[str, Any]:
        """Run ONE read-only DuckDB SELECT for ad-hoc analysis.

        Prefer get_metric for standard KPIs. Results are capped (default 100, max set by server).
        Restricted columns and non-SELECT statements are rejected with an explanation.
        """
        def body():
            cap = max(1, min(max_rows, app.settings.max_rows))
            checked = check_sql(sql, app.policy, app.table_columns, app.semantic.covering_metrics())
            res = app.wh.query(checked.sql, max_rows=cap)
            return {"columns": res.columns, "rows": res.rows, "row_count": len(res.rows),
                    "truncated": res.truncated, "tables": checked.tables}
        return app.run("run_sql", {"sql": sql, "max_rows": max_rows}, body)

    @mcp.tool(annotations=READ_ONLY)
    def list_metrics() -> dict[str, Any]:
        """List governed metrics available to your role, their definitions and allowed dimensions."""
        def body():
            out = []
            for m in app.semantic.metrics.values():
                if not app.policy.can_use_metric(m.name):
                    continue
                src = app.semantic.sources[m.source]
                out.append({"metric": m.name, "description": m.description, "source": m.source,
                            "time_column": src.time_column, "dimensions": list(src.dimensions)})
            return {"metrics": out, "row_count": len(out)}
        return app.run("list_metrics", {}, body)

    @mcp.tool(annotations=READ_ONLY)
    def get_metric(
        metrics: list[str],
        group_by: list[str] | None = None,
        filters: dict[str, str | list[str]] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Compute governed metrics with their single approved definition.

        Args:
            metrics: metric names from list_metrics (all must share one source).
            group_by: dimensions to break down by, e.g. ["retailer_name", "mechanic"].
            filters: exact-match filters, e.g. {"brand": "Neurex", "channel": ["Grocery", "Online"]}.
            start_date / end_date: inclusive YYYY-MM-DD bounds on the source's time column.
        """
        args = {"metrics": metrics, "group_by": group_by, "filters": filters,
                "start_date": start_date, "end_date": end_date}

        def body():
            denied = [m for m in metrics if not app.policy.can_use_metric(m)]
            if denied:
                raise ToolError(f"Role '{app.policy.name}' may not use metric(s) {denied}.")
            sql, params = app.semantic.build(metrics, group_by, filters, start_date, end_date)
            res = app.wh.query(sql, params, max_rows=app.settings.max_rows)
            return {"records": res.as_records(), "row_count": len(res.rows), "truncated": res.truncated,
                    "definitions": {m: app.semantic.metrics[m].description for m in metrics}}
        return app.run("get_metric", args, body)

    @mcp.tool(annotations=READ_ONLY)
    def analyze_promotion(promo_id: str) -> dict[str, Any]:
        """Post-event analysis for one promotion: uplift, dip, trade spend, ROI, and a plain verdict."""
        def body():
            if not app.policy.can_use_metric("promo_roi"):
                raise ToolError(f"Role '{app.policy.name}' may not view promotion financials.")
            res = app.wh.query("SELECT * FROM v_promo_performance WHERE promo_id = ?", [promo_id], max_rows=1)
            if not res.rows:
                raise ToolError(f"Promotion '{promo_id}' not found.")
            p = res.as_records()[0]
            roi = p["gross_roi"] or 0
            verdict = ("Paid back: incremental margin exceeded trade spend." if roi >= 1 else
                       "Marginal: recovered most but not all of the investment." if roi >= 0.8 else
                       "Did not pay back: margin from extra volume was well below the cost.")
            dip_share = (abs(p["post_promo_dip_units"]) / p["gross_incremental_units"]
                         if p["gross_incremental_units"] else 0)
            notes = []
            if p["discount_pct"] >= 0.45:
                notes.append("Deep discount (45%+): most of the spend subsidises baseline volume.")
            if dip_share > 0.2:
                notes.append(f"{dip_share:.0%} of the uplift was given back in the post-promo dip (pantry loading).")
            if p["channel"] == "Discounter":
                notes.append("Discounter channel is EDLP-led; promotions there are usually low-ROI.")
            if not (p["feature"] or p["display"]):
                notes.append("No feature or display support: price-only promos tend to lift less.")
            p["verdict"] = verdict
            p["notes"] = notes
            p["row_count"] = 1
            for k in ("start_week", "end_week"):
                p[k] = str(p[k])[:10]
            return p
        return app.run("analyze_promotion", {"promo_id": promo_id}, body)

    @mcp.resource("cpg://glossary", name="CPG glossary", mime_type="text/markdown")
    def glossary() -> str:
        """Business definitions: sell-in vs sell-out, baseline, uplift, trade spend, promo ROI, ship attainment."""
        return (PACKAGE_DIR / "knowledge" / "cpg_glossary.md").read_text()

    @mcp.prompt()
    def promo_post_event_review(promo_id: str) -> str:
        """Run a structured post-event review of one promotion."""
        return (
            f"Do a post-event review of promotion {promo_id}.\n"
            "1. Call analyze_promotion for the headline numbers.\n"
            "2. Compare it with similar events (same retailer and mechanic) using get_metric on promo_roi.\n"
            "3. Explain what drove the result (depth, support, dip, channel).\n"
            "4. Recommend keep / fix / stop, with one concrete change for the next event."
        )

    @mcp.prompt()
    def ship_attainment_review(month_start: str) -> str:
        """Explain shipment performance vs target for a month (YYYY-MM-01)."""
        return (
            f"Review ship attainment for the month starting {month_start}.\n"
            "1. get_metric ship_attainment_pct and case_fill_rate by retailer_name and brand for that month.\n"
            "2. Flag anything below 0.95 attainment or 0.95 fill rate.\n"
            "3. Check whether promotions the following month explain forward buying (sell-in ahead of sell-out).\n"
            "4. Summarise in five bullets for a sales director."
        )

    return mcp


def main() -> None:
    create_server().run()
