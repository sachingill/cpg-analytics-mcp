# cpg-analytics-mcp: governed MCP server over stdio.
#   docker build -t cpg-analytics-mcp .
#   docker run -i --rm -e CPG_MCP_ROLE=analyst cpg-analytics-mcp

# ---- Build stage: resolve dependencies from uv.lock and generate the synthetic DB ----
FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# Then the project itself.
COPY src ./src
RUN uv sync --locked --no-dev

# Deterministic synthetic data (SEED=42), baked in so the image runs with no setup step.
RUN .venv/bin/cpg-mcp-generate

# ---- Runtime stage: slim image, no uv or build tooling, non-root user ----
FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CPG_MCP_DB=/app/data/cpg.duckdb \
    CPG_MCP_AUDIT_LOG=/app/logs/audit.jsonl \
    CPG_MCP_ROLE=analyst

USER app

# stdio transport: run with `docker run -i` so the MCP client can write to stdin.
ENTRYPOINT ["cpg-analytics-mcp"]
