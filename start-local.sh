#!/usr/bin/env bash
# --------------------------------------------------------------------------
# start-local.sh — Run the immune system in LOCAL mode
#
# Flow:  InfluxDB (docker)  ◀──direct──  main.py (InfluxStore)
#
# The client talks to InfluxDB directly — no server container.
# Good for development and debugging.
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env -------------------------------------------------------------------
if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

export INFLUXDB_URL="${INFLUXDB_URL:-http://localhost:8086}"
export INFLUXDB_ORG="${INFLUXDB_ORG:-appd}"
export INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-immune_system}"
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-ai-agent-immune-system}"

if [[ -z "${INFLUXDB_TOKEN:-}" ]]; then
    echo "⚠  INFLUXDB_TOKEN is not set — app will fall back to in-memory mode."
    echo "   Set it in .env or export it before running."
fi

# Start infra (InfluxDB + OTEL only, no server) --------------------------------
echo "▶ Starting InfluxDB + OTEL collector …"
docker compose up -d influxdb otel-collector

echo "▶ Waiting for InfluxDB to be healthy …"
for i in $(seq 1 30); do
    if curl -sf http://localhost:8086/health > /dev/null 2>&1; then
        echo "  InfluxDB is ready."
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "✗ InfluxDB did not become healthy in 30 s. Check docker logs."
        exit 1
    fi
    sleep 1
done

# Run client directly (InfluxStore) -------------------------------------------
echo ""
echo "▶ Starting immune system (local / InfluxStore mode)"
echo "  Dashboard: http://localhost:8090"
echo ""
exec python main.py "$@"
