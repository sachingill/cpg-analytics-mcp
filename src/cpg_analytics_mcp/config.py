"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    role: str
    audit_log: Path
    max_rows: int
    query_timeout_s: float
    roles_file: Path
    metrics_file: Path

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            db_path=Path(env("CPG_MCP_DB", PROJECT_ROOT / "data" / "cpg.duckdb")),
            role=env("CPG_MCP_ROLE", "analyst"),
            audit_log=Path(env("CPG_MCP_AUDIT_LOG", PROJECT_ROOT / "logs" / "audit.jsonl")),
            max_rows=int(env("CPG_MCP_MAX_ROWS", "200")),
            query_timeout_s=float(env("CPG_MCP_TIMEOUT_S", "10")),
            roles_file=Path(env("CPG_MCP_ROLES_FILE", PACKAGE_DIR / "policies" / "roles.yaml")),
            metrics_file=Path(env("CPG_MCP_METRICS_FILE", PACKAGE_DIR / "policies" / "metrics.yaml")),
        )
