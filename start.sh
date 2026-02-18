#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables from .env if present (copy .env.example → .env)
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
    set +a
fi

# InfluxDB connection (must match observability/docker-compose.yml)
export INFLUXDB_URL="${INFLUXDB_URL:-http://localhost:8086}"
export INFLUXDB_ORG="${INFLUXDB_ORG:-appd}"
export INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-immune_system}"

# INFLUXDB_TOKEN must be set in .env or environment — do NOT hardcode secrets
if [[ -z "${INFLUXDB_TOKEN:-}" ]]; then
    echo "WARNING: INFLUXDB_TOKEN is not set. Set it in .env or export it."
    echo "         The app will fall back to in-memory mode without it."
fi

# OTEL collector (optional)
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-ai-agent-immune-system}"

cd "$SCRIPT_DIR"
exec python main.py "$@"
