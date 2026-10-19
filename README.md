# cpg-analytics-mcp

[![CI](https://github.com/sachingill/cpg-analytics-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/sachingill/cpg-analytics-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![MCP](https://img.shields.io/badge/MCP-SDK%20v2-purple)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A production-minded **MCP server** that gives AI assistants *governed, read-only* access to CPG
commercial data: sell-out, trade promotions (post-event analysis and ROI), and sell-in vs target.

Built on fictional health & hygiene brands. No client data.

> Ask an LLM: *"Which promotions at MegaMart didn't pay back last quarter, and why?"*
> It answers using one approved ROI definition, with every query checked by guardrails and written to an audit log.

## Why this exists

Demo agents talk to a database. Production agents need:

| Concern | How this server handles it |
|---|---|
| One version of the truth | **Semantic layer** ([metrics.yaml](src/cpg_analytics_mcp/policies/metrics.yaml)): the agent picks metrics and dimensions, the server writes the SQL |
| Least privilege | **Role policies** ([roles.yaml](src/cpg_analytics_mcp/policies/roles.yaml)): table allowlists, blocked columns, metric allowlists |
| Governed metrics | An analyst can't read unit cost, but *can* get promo ROI computed by approved logic |
| LLM-written SQL | Parsed with sqlglot (not regex): one SELECT only, no DDL/DML, no file-reading functions, no `SELECT *` over restricted tables |
| Blast radius | Read-only DB connection, external access disabled, row cap, query timeout |
| Accountability | JSONL **audit log** of every call: role, args, status, duration |
| Safe errors | Policy violations explain themselves; internal errors never leak stack traces to the model |

## Quick start

```bash
uv sync
uv run cpg-mcp-generate      # builds data/cpg.duckdb (2 years, 5 retailers, 20 SKUs, ~500 promos)
uv run pytest -q             # 22 tests: guardrails, RBAC, semantic layer, end-to-end tools
```

### Connect an LLM client

Any MCP-compatible LLM client can launch the server over stdio.

**Claude Code** (one command):

```bash
claude mcp add cpg-analytics -e CPG_MCP_ROLE=analyst -- uv run --directory /path/to/cpg-analytics-mcp cpg-analytics-mcp
```

**Claude Desktop and other MCP clients** use the standard `mcpServers` config format:

```json
{
  "mcpServers": {
    "cpg-analytics": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/cpg-analytics-mcp", "cpg-analytics-mcp"],
      "env": { "CPG_MCP_ROLE": "analyst" }
    }
  }
}
```

Switch `CPG_MCP_ROLE` to `sales` or `finance` to see the access policy change.

### Run with Docker

The image bakes in the synthetic database and runs as a non-root user. `-i` keeps stdin open for the stdio transport.

```bash
docker build -t cpg-analytics-mcp .
python3 scripts/smoke_test.py docker run -i --rm cpg-analytics-mcp   # handshake + tool calls
```

Point an MCP client at the container instead of `uv`:

```json
{
  "mcpServers": {
    "cpg-analytics": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "CPG_MCP_ROLE=analyst", "cpg-analytics-mcp"]
    }
  }
}
```

The audit log is written inside the container (`/app/logs/audit.jsonl`), so with `--rm` it is lost when the container exits. Mount a volume on `/app/logs` to keep it.

## Tools, resources, prompts

| Type | Name | Purpose |
|---|---|---|
| Tool | `list_datasets` | Tables/views the role can see |
| Tool | `describe_table` | Columns (restricted ones hidden) |
| Tool | `list_metrics` | Governed metrics + allowed dimensions |
| Tool | `get_metric` | Compute metrics by dimensions/filters/date range |
| Tool | `analyze_promotion` | Post-event analysis + verdict for one promo |
| Tool | `run_sql` | Guarded ad-hoc SELECT (fallback) |
| Resource | `cpg://glossary` | Business definitions ([glossary](src/cpg_analytics_mcp/knowledge/cpg_glossary.md)) |
| Prompt | `promo_post_event_review` | Structured promo review workflow |
| Prompt | `ship_attainment_review` | Monthly sell-in vs target review |

All tools are annotated read-only / non-destructive / idempotent.

## Data model

```
dim_week ─┐                     ┌─ fact_promotions (mechanic, depth, funding, feature/display, fixed cost)
dim_product (brand, PPG, cost) ─┼─ fact_sales      (weekly sell-out, baseline, promo link)
dim_retailer (channel) ─────────┼─ fact_shipments  (weekly sell-in: ordered vs shipped)
                                └─ fact_targets    (monthly target by retailer x brand)

Views:  v_sales_enriched · v_promo_performance (uplift, dip, spend, ROI) · v_ship_attainment
```

The generator encodes real commercial behaviour so the analysis has something true to find:
seasonality by category, diminishing returns to discount depth, feature/display lift, retailer
co-funding, post-promo dip (strongest in storable categories), forward buying ahead of promos,
and an EDLP discounter whose shoppers barely respond to promotions.

## Configuration

| Env var | Default | |
|---|---|---|
| `CPG_MCP_ROLE` | `analyst` | `analyst` / `sales` / `finance` |
| `CPG_MCP_DB` | `data/cpg.duckdb` | |
| `CPG_MCP_MAX_ROWS` | `200` | Hard cap per query |
| `CPG_MCP_TIMEOUT_S` | `10` | Query timeout |
| `CPG_MCP_AUDIT_LOG` | `logs/audit.jsonl` | |

## Docs

- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
