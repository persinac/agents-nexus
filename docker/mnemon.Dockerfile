FROM python:3.14-slim

WORKDIR /app

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install mnemon with postgres + embedding extras
COPY mnemon/pyproject.toml mnemon/uv.lock ./
RUN uv sync --extra mcp --no-dev --frozen

# Copy source so uv run can find the package
COPY mnemon/agent_memory/ agent_memory/

# Copy flush script
COPY tmux/mac/tmux-scripts/flush-events.py /usr/local/bin/flush-events.py

# Flush loop: drain memory-events.jsonl into Postgres every 120 seconds.
# HOME is overridden in compose to /host-home so Path.home()/.tmux resolves correctly.
CMD ["/bin/sh", "-c", "while true; do uv run python /usr/local/bin/flush-events.py; sleep 120; done"]
