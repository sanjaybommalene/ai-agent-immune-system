#!/usr/bin/env bash
# --------------------------------------------------------------------------
# start-gateway.sh — Run the immune system in GATEWAY mode (passive observation)
#
# Flow:  Customer Agents ──► LLM Gateway (port 4000) ──► OpenAI / Azure / etc.
#                                  │
#                                  ▼
#                           Immune System Core
#                      (baselines, detection, alerts)
#
# Agents point their OPENAI_BASE_URL at http://localhost:4000/v1
# No agent code changes required — the gateway observes passively.
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env -------------------------------------------------------------------
if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

export LLM_UPSTREAM_URL="${LLM_UPSTREAM_URL:-https://api.openai.com}"
export GATEWAY_PORT="${GATEWAY_PORT:-4000}"
export INFLUXDB_URL="${INFLUXDB_URL:-http://localhost:8086}"
export INFLUXDB_ORG="${INFLUXDB_ORG:-appd}"
export INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-immune_system}"

if [[ -z "${INFLUXDB_TOKEN:-}" ]]; then
    echo "Note: INFLUXDB_TOKEN not set — gateway will run in memory-only mode."
fi

# Start infra (InfluxDB + OTEL + Gateway) -------------------------------------
echo "▶ Starting InfluxDB + OTEL + LLM Gateway …"
docker compose up -d influxdb otel-collector gateway

echo "▶ Waiting for Gateway to be healthy …"
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${GATEWAY_PORT}/health" > /dev/null 2>&1; then
        echo "  Gateway is ready."
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "✗ Gateway did not become healthy in 30 s. Check docker logs."
        exit 1
    fi
    sleep 1
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  LLM Gateway running on http://localhost:${GATEWAY_PORT}"
echo ""
echo "  Point your agents here:"
echo "    export OPENAI_BASE_URL=http://localhost:${GATEWAY_PORT}/v1"
echo ""
echo "  Management APIs:"
echo "    GET  /health                      Health check"
echo "    GET  /api/gateway/agents          Discovered agents"
echo "    GET  /api/gateway/stats           Detection stats"
echo "    GET  /api/gateway/policies        Active policies"
echo "    GET  /api/gateway/agent/{id}/vitals   Agent vitals"
echo "    GET  /api/gateway/agent/{id}/baseline Agent baseline"
echo "═══════════════════════════════════════════════════════════════"
