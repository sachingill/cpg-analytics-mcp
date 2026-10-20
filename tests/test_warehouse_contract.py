import duckdb
import pytest

from cpg_analytics_mcp.data.generate import build
from cpg_analytics_mcp.testing.warehouse_contract import WarehouseContract
from cpg_analytics_mcp.warehouse import DuckDBWarehouse


@pytest.fixture(scope="module")
def duckdb_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("contract") / "cpg.duckdb"
    build(path)
    return path


class TestDuckDBWarehouse(WarehouseContract):
    @pytest.fixture(autouse=True)
    def _db(self, duckdb_path):
        self.db_path = duckdb_path

    def make_warehouse(self, timeout_s):
        return DuckDBWarehouse(self.db_path, timeout_s)

    def test_connection_itself_is_read_only(self):
        # defense in depth: even SQL that bypassed query() and the guardrails cannot write
        wh = self.make_warehouse(timeout_s=30)
        with pytest.raises(duckdb.Error, match="read-only"):
            wh._con.execute("CREATE TABLE probe AS SELECT 1")

    def test_external_access_is_disabled(self):
        wh = self.make_warehouse(timeout_s=30)
        with pytest.raises(duckdb.Error):
            wh._con.execute("SELECT * FROM read_csv('/etc/hosts')").fetchall()


class _UppercaseColumns(DuckDBWarehouse):
    """Mimics a Snowflake adapter that forgot to normalise identifier case."""

    def query(self, sql, params=None, max_rows=200):
        res = super().query(sql, params, max_rows)
        res.columns = [c.upper() for c in res.columns]
        return res


class _LeaksDriverTimeout(DuckDBWarehouse):
    """Mimics an adapter that lets the driver's own timeout error escape."""

    def query(self, sql, params=None, max_rows=200):
        raise duckdb.InterruptException("driver-specific timeout")


def test_contract_catches_uppercase_columns(duckdb_path):
    contract = WarehouseContract()
    with pytest.raises(AssertionError):
        contract.test_result_columns_are_lowercase_and_values_plain_python(_UppercaseColumns(duckdb_path, 30))


def test_contract_catches_leaked_driver_timeout(duckdb_path):
    contract = WarehouseContract()
    contract.make_warehouse = lambda timeout_s: _LeaksDriverTimeout(duckdb_path, timeout_s)
    with pytest.raises(duckdb.InterruptException):
        contract.test_timeout_raises_query_timeout()
