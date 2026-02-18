#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# InfluxDB connection (must match observability/docker-compose.yml)
export INFLUXDB_URL="http://localhost:8086"
export INFLUXDB_TOKEN="hackathon-influx-token-1234567890"
export INFLUXDB_ORG="appd"
export INFLUXDB_BUCKET="immune_system"

# OTEL collector (optional – enable if the collector container is running)
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
export OTEL_SERVICE_NAME="ai-agent-immune-system"

cd "$SCRIPT_DIR"
exec python main.py "$@"
