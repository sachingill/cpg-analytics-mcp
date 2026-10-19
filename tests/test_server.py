import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cpg_analytics_mcp.config import Settings
from cpg_analytics_mcp.data.generate import build
from cpg_analytics_mcp.dialect import to_dialect
from cpg_analytics_mcp.governance import GuardrailError, check_sql, load_role
from cpg_analytics_mcp.semantic import MetricError, SemanticLayer
from cpg_analytics_mcp.server import create_server
from cpg_analytics_mcp.warehouse import load_warehouse


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


def test_guardrails_check_in_the_warehouse_dialect(settings):
    analyst = load_role(settings.roles_file, "analyst")
    # parsed and re-emitted as Databricks SQL (STRING), not DuckDB (VARCHAR/TEXT)
    checked = check_sql("SELECT CAST(brand AS STRING) FROM dim_product", analyst, COLS, dialect="databricks")
    assert "AS STRING" in checked.sql
    with pytest.raises(GuardrailError, match="restricted"):
        check_sql("SELECT cogs_per_unit FROM dim_product", analyst, COLS, dialect="snowflake")


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


def test_semantic_sql_translates_per_warehouse(settings):
    sql, params = SemanticLayer(settings.metrics_file).build(["promo_roi"], ["channel"], {"brand": "Neurex"})
    assert to_dialect(sql, "duckdb") == sql
    databricks = to_dialect(sql, "databricks")
    assert "CAST(brand AS STRING) IN (?)" in databricks  # placeholders survive translation
    assert databricks.count("?") == len(params)


def test_unknown_warehouse_is_explained(settings):
    with pytest.raises(ValueError, match="Unknown warehouse 'oracle'.*mcp-warehouse-adapters"):
        load_warehouse(replace(settings, warehouse="oracle"))


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


def test_range_roi_reflects_cannibalisation_and_halo(settings):
    out = call(create_server(settings), "get_metric", metrics=["promo_roi", "range_roi"], group_by=["brand"])
    by_brand = {r["brand"]: r for r in out["records"]}
    # substitutes (liquid vs pods) cannibalise: the range earns less than the promoted PPG
    assert by_brand["FreshFold"]["range_roi"] < by_brand["FreshFold"]["promo_roi"]
    # complements (toothpaste + mouthwash) get a halo: the range earns more
    assert by_brand["DentaGuard"]["range_roi"] > by_brand["DentaGuard"]["promo_roi"]


def test_display_amplifies_halo(settings):
    out = call(create_server(settings), "get_metric", metrics=["cross_product_margin", "promo_count"],
               group_by=["display"], filters={"brand": "DentaGuard", "cross_effect": ["halo", "cannibalisation"]})
    per_promo = {r["display"]: r["cross_product_margin"] / r["promo_count"] for r in out["records"]}
    assert per_promo[True] > per_promo[False] > 0


def test_promo_economics_stay_calibrated(settings):
    # Cross effects only touch clean sibling weeks, so each promo's own economics are unchanged.
    out = call(create_server(settings), "run_sql",
               sql="SELECT AVG(CASE WHEN gross_roi >= 1 THEN 1.0 ELSE 0 END) AS payback FROM v_promo_performance")
    assert 0.20 <= out["rows"][0][0] <= 0.35
    out = call(create_server(settings), "get_metric", metrics=["promo_roi"], group_by=["discount_band"])
    roi = {r["discount_band"]: r["promo_roi"] for r in out["records"]}
    assert roi["<20%"] > roi["30-44%"] > roi["45%+"]


def test_analyze_promotion_explains_cannibalisation(settings):
    server = create_server(settings)
    found = call(server, "run_sql", sql="SELECT promo_id FROM v_promo_performance "
                 "WHERE brand = 'FreshFold' AND cross_effect = 'cannibalisation' ORDER BY promo_id LIMIT 1")
    out = call(server, "analyze_promotion", promo_id=found["rows"][0][0])
    assert out["cross_effect"] == "cannibalisation" and out["range_roi"] < out["gross_roi"]
    assert any(n.startswith("Cannibalisation:") for n in out["notes"])


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
