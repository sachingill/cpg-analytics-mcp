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
- [ ] Dockerfile + GitHub Actions (lint, tests)
- [ ] Architecture diagram image + 2-minute demo video

## Project 2 (Nov): evals and observability
- [ ] Golden question set (50 business questions with expected answers)
- [ ] Eval harness: accuracy, tool-choice correctness, guardrail adherence; publish benchmark
- [ ] Tracing (OpenTelemetry), cost and latency per question

## Claude Certified Architect – Foundations: where this project covers each domain

| Exam domain | Weight | Covered here by |
|---|---|---|
| Agentic architecture & orchestration | 27% | Tool design for multi-step analysis; workflow prompts; metric-first vs SQL-fallback paths |
| Claude Code configuration & workflows | 20% | `claude mcp add` setup; build this project with Claude Code; project CLAUDE.md (todo) |
| Prompt engineering & structured output | 20% | Server instructions; tool docstrings as prompts; structured tool results |
| Tool design & MCP integration | 18% | Tools vs resources vs prompts; annotations; self-explaining errors; stdio transport |
| Context management & reliability | 15% | Row caps, compact results, glossary as a resource, timeouts, audit trail |
