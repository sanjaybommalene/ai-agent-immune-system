#!/usr/bin/env bash
# --------------------------------------------------------------------------
# start-server.sh — Run the immune system in SERVER mode
#
# Flow:  InfluxDB (docker)  ◀──  server/app.py (docker)  ◀──HTTP──  main.py (ApiStore)
#
# docker compose brings up InfluxDB + OTEL + the Server API container.
# The client connects to the server via ApiStore — same as a real deployment.
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env -------------------------------------------------------------------
if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

if [[ -z "${INFLUXDB_TOKEN:-}" ]]; then
    echo "✗ INFLUXDB_TOKEN must be set in .env or environment."
    echo "  Copy .env.example → .env and fill in the token."
    exit 1
fi

SERVER_PORT="${SERVER_PORT:-5000}"
export SERVER_API_BASE_URL="http://localhost:${SERVER_PORT}"
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-ai-agent-immune-system}"

# Forward API key to client if set
if [[ -n "${SERVER_API_KEY:-}" ]]; then
    export SERVER_API_KEY
fi

# Build & start full stack (InfluxDB + OTEL + Server) -------------------------
echo "▶ Building and starting full stack (InfluxDB + OTEL + Server API) …"
docker compose up -d --build

echo "▶ Waiting for Server API to be healthy …"
for i in $(seq 1 60); do
    if curl -sf "${SERVER_API_BASE_URL}/api/v1/health" > /dev/null 2>&1; then
        echo "  Server API is ready at ${SERVER_API_BASE_URL}"
        break
    fi
    if [[ $i -eq 60 ]]; then
        echo "✗ Server API did not become healthy in 60 s."
        echo "  Check: docker compose logs server"
        exit 1
    fi
    sleep 1
done

# Run client with ApiStore -----------------------------------------------------
echo ""
echo "▶ Starting immune system (server / ApiStore mode)"
echo "  Server API:  ${SERVER_API_BASE_URL}"
echo "  Dashboard:   http://localhost:8090"
echo ""
echo "  To stop infrastructure:  docker compose down"
echo ""
exec python main.py "$@"
