# Agent context

Context for LLM coding agents working in this repo.

## What this is
A production-minded MCP server giving AI assistants governed, read-only access to synthetic CPG
commercial data (sell-out, trade promotions / promo ROI, sell-in vs target). Portfolio project:
the point is to show the gap between an AI demo and production (governance, trust, safety).
All data is synthetic and fictional — never add real client data, names, or code.

## Commands
```bash
uv sync                    # install
uv run cpg-mcp-generate    # build data/cpg.duckdb (deterministic, SEED=42)
uv run pytest -q           # 22 tests; must stay green
uv run cpg-analytics-mcp   # run server over stdio
```

## Layout
- `src/cpg_analytics_mcp/server.py` — MCPServer (MCP Python SDK **v2**: `from mcp.server.mcpserver import MCPServer`; `FastMCP` no longer exists). Tools, glossary resource, prompts. `App.run()` wraps every tool with error handling + audit.
- `governance.py` — role policies, sqlglot AST guardrails (allowlist: one SELECT; no DML/DDL, no file functions; table/column rules), JSONL audit log.
- `semantic.py` + `policies/metrics.yaml` — governed metrics: identifiers from allowlists, values as bound params.
- `policies/roles.yaml` — analyst / sales / finance. Governed-metric pattern: analyst can't read `cogs_per_unit` but can get `promo_roi`.
- `warehouse.py` — DuckDB, read-only, external access off, config locked, row cap, timeout via interrupt.
- `data/generate.py` — synthetic world + SQL views (`v_sales_enriched`, `v_promo_performance`, `v_ship_attainment`). Calibrated so ~25% of promos pay back, ROI falls with discount depth, feature+display helps, discounter (EDLP) worst.
- `knowledge/cpg_glossary.md` — business definitions served as `cpg://glossary`.
- `docs/architecture.md`, `docs/roadmap.md` — design decisions and next steps.

## Conventions
- Logs go to stderr only (stdout is the stdio transport).
- Tool errors must explain the policy and suggest the right tool, so the model self-corrects; never leak internals.
- Keep changes surgical; explain before restructuring. Add a test for every guardrail or metric change.
- If generator economics change, re-check: payback rate ~20–35%, ROI decreasing with depth, Discounter < Grocery.

## Open decisions
- Schema layout: everything is in DuckDB `main`. Leaning towards layered schemas `raw` / `curated` / `semantic` to mirror Unity Catalog — not decided yet.
