import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cpg_analytics_mcp.config import Settings
from cpg_analytics_mcp.data.generate import build
from cpg_analytics_mcp.governance import GuardrailError, check_sql, load_role
from cpg_analytics_mcp.semantic import MetricError, SemanticLayer
from cpg_analytics_mcp.server import create_server


@pytest.fixture(scope="session")
def db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("data") / "cpg.duckdb"
    build(path)
    return path


@pytest.fixture
def settings(db, tmp_path) -> Settings:
    return replace(Settings.from_env(), db_path=db, audit_log=tmp_path / "audit.jsonl")


def call(server, tool, **args):
    result = asyncio.run(server.call_tool(tool, args))
    return result.structured_content


COLS = {
    "dim_product": {"sku_id", "brand", "list_price", "cogs_per_unit"},
    "fact_sales": {"sku_id", "units", "promo_id"},
    "fact_targets": {"brand", "target_value"},
}


# --- SQL guardrails -------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DROP TABLE fact_sales",
    "DELETE FROM fact_sales",
    "INSERT INTO fact_sales VALUES (1)",
    "SELECT 1; SELECT 2",
    "SELECT * FROM read_csv('/etc/passwd')",
    "COPY fact_sales TO 'out.csv'",
    "ATTACH 'other.db'",
    "SELECT * FROM secret_table",
])
def test_guardrails_reject_unsafe_sql(sql, settings):
    policy = load_role(settings.roles_file, "finance")
    with pytest.raises(GuardrailError):
        check_sql(sql, policy, COLS)


def test_blocked_column_and_star_for_analyst(settings):
    analyst = load_role(settings.roles_file, "analyst")
    with pytest.raises(GuardrailError, match="restricted"):
        check_sql("SELECT sku_id, cogs_per_unit FROM dim_product", analyst, COLS)
    with pytest.raises(GuardrailError, match=r"SELECT \*"):
        check_sql("SELECT * FROM dim_product", analyst, COLS)
    # COUNT(*) and explicit safe columns are fine
    check_sql("SELECT brand, COUNT(*) FROM dim_product GROUP BY brand", analyst, COLS)


def test_blocked_column_hint_names_usable_metric(settings):
    # Analyst may use promo_roi, so the error should point at it.
    analyst = load_role(settings.roles_file, "analyst")
    covering = SemanticLayer(settings.metrics_file).covering_metrics()
    with pytest.raises(GuardrailError, match=r"get_metric with .*promo_roi"):
        check_sql("SELECT cogs_per_unit FROM dim_product", analyst, COLS, covering)


def test_blocked_column_hint_never_suggests_denied_metric(settings):
    # Sales may not use any cost-based metric, so suggesting get_metric would be a dead end.
    sales = load_role(settings.roles_file, "sales")
    covering = SemanticLayer(settings.metrics_file).covering_metrics()
    with pytest.raises(GuardrailError) as exc:
        check_sql("SELECT cogs_per_unit FROM dim_product", sales, COLS, covering)
    assert "get_metric" not in str(exc.value)
    assert "No governed metric exposes it" in str(exc.value)


def test_ctes_allowed(settings):
    finance = load_role(settings.roles_file, "finance")
    q = check_sql("WITH x AS (SELECT sku_id FROM fact_sales) SELECT COUNT(*) FROM x", finance, COLS)
    assert q.tables == ["fact_sales"]


def test_sales_role_cannot_read_targets(settings):
    sales = load_role(settings.roles_file, "sales")
    with pytest.raises(GuardrailError, match="may not read"):
        check_sql("SELECT brand FROM fact_targets", sales, COLS)


# --- Semantic layer -------------------------------------------------------------

def test_semantic_rejects_unknown_dimension_and_mixed_sources(settings):
    sl = SemanticLayer(settings.metrics_file)
    with pytest.raises(MetricError, match="not available"):
        sl.build(["sales_units"], group_by=["sku_id; DROP TABLE x"])
    with pytest.raises(MetricError, match="different sources"):
        sl.build(["sales_units", "promo_roi"])


def test_semantic_binds_filter_values(settings):
    sql, params = SemanticLayer(settings.metrics_file).build(
        ["sales_units"], ["brand"], {"brand": "Neurex' OR 1=1 --"}, "2026-01-01")
    assert "Neurex" not in sql and params[0] == "Neurex' OR 1=1 --"


# --- End-to-end tools -----------------------------------------------------------

def test_get_metric_promo_roi_by_channel(settings):
    out = call(create_server(settings), "get_metric", metrics=["promo_roi", "promo_count"], group_by=["channel"])
    by_channel = {r["channel"]: r for r in out["records"]}
    assert set(by_channel) == {"Grocery", "Discounter", "Convenience", "Pharmacy", "Online"}
    # domain sanity: EDLP discounter promos return less than hi-lo grocery promos
    assert by_channel["Discounter"]["promo_roi"] < by_channel["Grocery"]["promo_roi"]


def test_analyze_promotion_and_audit(settings):
    server = create_server(settings)
    out = call(server, "analyze_promotion", promo_id="P0001")
    assert out["promo_id"] == "P0001" and "verdict" in out
    assert out["total_trade_spend"] == pytest.approx(out["variable_trade_spend"] + out["fixed_cost"], abs=0.02)
    lines = [json.loads(x) for x in settings.audit_log.read_text().splitlines()]
    assert lines[-1]["tool"] == "analyze_promotion" and lines[-1]["status"] == "ok"


def test_run_sql_rejection_is_audited(settings):
    server = create_server(settings)
    with pytest.raises(ToolError, match="restricted"):
        call(server, "run_sql", sql="SELECT cogs_per_unit FROM dim_product")
    last = json.loads(settings.audit_log.read_text().splitlines()[-1])
    assert last["status"] == "rejected"


def test_sales_role_metric_denied(settings):
    server = create_server(replace(settings, role="sales"))
    with pytest.raises(ToolError, match="may not use"):
        call(server, "get_metric", metrics=["promo_roi"])


def test_sales_blocked_column_error_end_to_end(settings):
    server = create_server(replace(settings, role="sales"))
    with pytest.raises(ToolError, match="requires additional access"):
        call(server, "run_sql", sql="SELECT sku_name, cogs_per_unit FROM dim_product")


def test_describe_hides_blocked_columns(settings):
    out = call(create_server(settings), "describe_table", table="dim_product")
    assert "cogs_per_unit" not in {c["name"] for c in out["columns"]}


def test_row_cap(settings):
    server = create_server(replace(settings, max_rows=5))
    out = call(server, "run_sql", sql="SELECT sku_id FROM fact_sales", max_rows=1000)
    assert out["row_count"] == 5 and out["truncated"] is True
