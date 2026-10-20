"""Contract tests every Warehouse adapter must pass (requires pytest and the synthetic dataset).

Usage in an adapter's test suite:

    from cpg_analytics_mcp.testing.warehouse_contract import WarehouseContract

    class TestSnowflake(WarehouseContract):
        def make_warehouse(self, timeout_s: float) -> Warehouse:
            return SnowflakeWarehouse(..., timeout_s=timeout_s)

These checks go through `query()` only. Each adapter should also test that its *connection or role*
cannot write (DuckDB: read-only file; Snowflake: role without write grants; Databricks: SELECT-only grants).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import sqlglot

from cpg_analytics_mcp.config import PACKAGE_DIR
from cpg_analytics_mcp.dialect import to_dialect
from cpg_analytics_mcp.semantic import SemanticLayer
from cpg_analytics_mcp.warehouse import QueryResult, QueryTimeout, Warehouse

EXPECTED_OBJECTS = {
    "dim_week": "table", "dim_product": "table", "dim_retailer": "table", "fact_promotions": "table",
    "fact_sales": "table", "fact_shipments": "table", "fact_targets": "table",
    "v_sales_enriched": "view", "v_promo_performance": "view", "v_ship_attainment": "view",
}
SCALAR_TYPES = (int, float, str, bool, dt.date, dt.datetime, type(None))  # never Decimal or driver types
SLOW_SQL = "SELECT COUNT(*) FROM fact_sales a, fact_sales b, fact_sales c WHERE a.units + b.units > c.units"


class WarehouseContract:
    """Subclass and implement make_warehouse(); pytest collects the inherited tests."""

    def make_warehouse(self, timeout_s: float) -> Warehouse:
        raise NotImplementedError

    @pytest.fixture
    def wh(self) -> Warehouse:
        return self.make_warehouse(timeout_s=30)

    def run(self, wh: Warehouse, canonical_sql: str, params: list | None = None, max_rows: int = 200) -> QueryResult:
        return wh.query(to_dialect(canonical_sql, wh.dialect), params, max_rows=max_rows)

    # --- identity and catalog -----------------------------------------------------

    def test_dialect_is_known_to_sqlglot(self, wh):
        sqlglot.Dialect.get_or_raise(wh.dialect)

    def test_catalog_lists_every_object_in_lowercase(self, wh):
        cat = wh.catalog()
        for name, kind in EXPECTED_OBJECTS.items():
            assert name in cat, f"missing {name}"
            assert cat[name]["type"] == kind
            assert cat[name]["columns"], f"{name} has no columns"
            assert all(c == c.lower() for c in cat[name]["columns"]), f"{name} columns not lowercase"
        assert "cogs_per_unit" in cat["dim_product"]["columns"]

    # --- query results ------------------------------------------------------------

    def test_result_columns_are_lowercase_and_values_plain_python(self, wh):
        res = self.run(wh, "SELECT brand, ROUND(AVG(list_price), 2) AS avg_price, COUNT(*) AS skus "
                           "FROM dim_product GROUP BY brand ORDER BY brand")
        assert res.columns == ["brand", "avg_price", "skus"]
        assert len(res.rows) == 5 and not res.truncated
        assert all(isinstance(v, SCALAR_TYPES) for row in res.rows for v in row)

    def test_placeholders_bind_values(self, wh):
        res = self.run(wh, "SELECT sku_id FROM dim_product WHERE brand = ? AND list_price > ?", ["Neurex", 3])
        assert len(res.rows) == 3  # Neurex Tablets 32 tabs, Neurex Gel 50 g and 100 g

    def test_row_cap_sets_truncated(self, wh):
        capped = self.run(wh, "SELECT sku_id FROM fact_sales", max_rows=3)
        assert len(capped.rows) == 3 and capped.truncated
        exact = self.run(wh, "SELECT retailer_id FROM dim_retailer", max_rows=5)
        assert len(exact.rows) == 5 and not exact.truncated

    def test_every_governed_metric_runs_after_translation(self, wh):
        semantic = SemanticLayer(Path(PACKAGE_DIR / "policies" / "metrics.yaml"))
        for name in semantic.metrics:
            sql, params = semantic.build([name])
            res = self.run(wh, sql, params)
            assert len(res.rows) == 1, name
            assert isinstance(res.rows[0][0], (int, float, type(None))), f"{name} is not a plain number"

    # --- failure modes ------------------------------------------------------------

    def test_timeout_raises_query_timeout(self):
        slow = self.make_warehouse(timeout_s=0.5)
        with pytest.raises(QueryTimeout):
            self.run(slow, SLOW_SQL)

    def test_writes_through_query_fail(self, wh):
        with pytest.raises(Exception) as exc:
            wh.query("CREATE TABLE contract_probe AS SELECT 1 AS x")
        assert not isinstance(exc.value, QueryTimeout)
        assert "contract_probe" not in wh.catalog()
