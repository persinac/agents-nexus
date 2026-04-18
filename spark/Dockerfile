FROM python:3.12-slim

WORKDIR /app

# Git is needed for webhook git-pull
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml .
COPY src/ src/
COPY config.yaml .

# Install dependencies
RUN uv sync --no-dev

# Default: run the MCP server
ENTRYPOINT ["uv", "run", "spark"]
CMD ["serve"]
