#!/usr/bin/env bash
# Flush buffered memory events to Postgres.
# Uses the agent-memory venv Python (has psycopg + dotenv).

AGENT_MEMORY_DIR="${AGENT_MEMORY_DIR:-$HOME/minions/minions-suite/agent-memory}"
PYTHON="$AGENT_MEMORY_DIR/.venv/bin/python3"

if [ ! -x "$PYTHON" ]; then
    exit 0
fi

exec "$PYTHON" "$HOME/.tmux/flush-events.py" >> "$HOME/.tmux/flush-events.log" 2>&1
