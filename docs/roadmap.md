# Roadmap

## Done (v0.1, week 1)
- [x] Synthetic CPG dataset with realistic promo economics
- [x] Semantic layer (14 governed metrics, 3 sources)
- [x] Role-based access (analyst / sales / finance) with blocked columns
- [x] sqlglot guardrails, read-only connection, row cap, timeout
- [x] JSONL audit log, safe error handling
- [x] Post-event promo analysis tool, glossary resource, two workflow prompts
- [x] 19 tests

## Week 2–3: production hardening
- [ ] Streamable HTTP transport + OAuth token verification; role from token claims
- [ ] Snowflake adapter (Snowpark / connector, key-pair auth, warehouse + role per user)
- [ ] Databricks adapter (SQL warehouse, Unity Catalog permissions as the source of truth)
- [ ] Cannibalisation + halo in the generator and promo analysis
- [x] Dockerfile + GitHub Actions (lint, tests, container smoke test)
- [ ] Architecture diagram image + 2-minute demo video

## Project 2 (Nov): evals and observability
- [ ] Golden question set (50 business questions with expected answers)
- [ ] Eval harness: accuracy, tool-choice correctness, guardrail adherence; publish benchmark
- [ ] Tracing (OpenTelemetry), cost and latency per question
- [ ] Close the metric-bypass gap: `run_sql` can re-aggregate governed columns with a different formula
      (e.g. ROI over variable spend only: Grocery 1.35 vs governed 1.02). Add an AST rule that rejects
      aggregates over metric-input columns and redirects to `get_metric`; measure tool-choice rate in evals.
