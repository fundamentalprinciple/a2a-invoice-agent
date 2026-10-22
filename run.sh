
#!/usr/bin/env bash
set -Eeuo pipefail

# Always run from the project root, regardless of where this script is called.
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "==> A2A Invoice Agent: local setup"

# 1. Check for uv.
if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv is not installed."
    echo "Install it using: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

# 2. Create local configuration on first run.
if [[ ! -f .env ]]; then
    if [[ ! -f .env.example ]]; then
        echo "Error: .env.example is missing."
        exit 1
    fi

    cp .env.example .env
    echo "Created .env from .env.example."
    echo "Edit .env and provide your AI Pipe API key and a secure A2A bearer token."
    echo "Then run ./run.sh again."
    exit 1
fi

# 3. Ensure required credentials are configured without displaying them.
for variable in AIPIPE_API_KEY A2A_BEARER_TOKEN; do
    if ! grep -Eq "^[[:space:]]*${variable}[[:space:]]*=[[:space:]]*[^[:space:]#]" .env; then
        echo "Error: ${variable} is missing or empty in .env."
        echo "Edit .env and try again."
        exit 1
    fi
done

if grep -Eq '^[[:space:]]*A2A_BEARER_TOKEN[[:space:]]*=[[:space:]]*["\047]?replace-with-a-long-random-secret' .env; then
    echo "Error: Replace the example A2A bearer token in .env."
    exit 1
fi

# 4. Install locked project dependencies and create the virtual environment.
echo "==> Installing dependencies..."
uv sync --locked

# 5. Start the local development server.
echo "==> Starting FastAPI at http://127.0.0.1:8000"
echo "==> Agent card: http://127.0.0.1:8000/.well-known/agent-card.json"
exec uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

