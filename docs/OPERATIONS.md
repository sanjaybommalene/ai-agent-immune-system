# OPERATIONS

## Purpose
Operational runbook for the AI Agent Immune System demo with InfluxDB + OpenTelemetry.

---

## Prerequisites
- Docker + Docker Compose installed
- Python virtualenv created at `venv/`
- Dependencies installed from `requirements.txt`

---

## Quick Start (2-minute demo)
1. Start observability stack:
```bash
cd /Users/sanbn/workspace/appd/hackathon/observability
docker compose up -d
```

2. Start demo app:
```bash
cd /Users/sanbn/workspace/appd/hackathon
source venv/bin/activate
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=hackathon-influx-token-1234567890 \
INFLUXDB_ORG=appd \
INFLUXDB_BUCKET=immune_system \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_METRIC_EXPORT_INTERVAL_MS=2000 \
OTEL_SERVICE_NAME=ai-agent-immune-system \
RUN_DURATION_SECONDS=120 \
python3 demo.py
```

3. Open dashboard:
- `http://localhost:8090`

---

## Quick Start (10-minute run)
```bash
cd /Users/sanbn/workspace/appd/hackathon
source venv/bin/activate
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=hackathon-influx-token-1234567890 \
INFLUXDB_ORG=appd \
INFLUXDB_BUCKET=immune_system \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_METRIC_EXPORT_INTERVAL_MS=5000 \
OTEL_SERVICE_NAME=ai-agent-immune-system \
RUN_DURATION_SECONDS=600 \
python3 demo.py
```

---

## Main App Run (non-demo)
```bash
cd /Users/sanbn/workspace/appd/hackathon
source venv/bin/activate
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=hackathon-influx-token-1234567890 \
INFLUXDB_ORG=appd \
INFLUXDB_BUCKET=immune_system \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_SERVICE_NAME=ai-agent-immune-system \
RUN_DURATION_SECONDS=1200 \
python3 main.py
```

---

## Environment Variables
Required for DB-backed mode:
- `INFLUXDB_URL` (example: `http://localhost:8086`)
- `INFLUXDB_TOKEN`
- `INFLUXDB_ORG`
- `INFLUXDB_BUCKET`

OTel (recommended):
- `OTEL_EXPORTER_OTLP_ENDPOINT` (example: `http://localhost:4318`)
- `OTEL_SERVICE_NAME` (example: `ai-agent-immune-system`)
- `OTEL_METRIC_EXPORT_INTERVAL_MS` (example: `2000` for short demos)

Runtime:
- `RUN_DURATION_SECONDS`

Notes:
- If Influx env vars are missing, app falls back to in-memory mode.
- Each run is isolated by `run_id` in Influx, so old data does not pollute new runs.

---

## Health Checks
Influx health:
```bash
curl -s http://localhost:8086/health
```

Dashboard status:
```bash
curl -s http://localhost:8090/api/status
```

Stats:
```bash
curl -s http://localhost:8090/api/stats
```

Agents:
```bash
curl -s http://localhost:8090/api/agents
```

Pending approvals:
```bash
curl -s http://localhost:8090/api/pending-approvals
```

Rejected approvals:
```bash
curl -s http://localhost:8090/api/rejected-approvals
```

---

## Shutdown
Stop app:
```bash
pkill -f "python3 demo.py" || true
pkill -f "python3 main.py" || true
```

Stop observability stack:
```bash
cd /Users/sanbn/workspace/appd/hackathon/observability
docker compose down
```

---

## Known Issues and Fixes

1. Approve/Reject actions appear to do nothing in demo
- Cause: dashboard loop reference missing.
- Fix: `demo.py` must call `dashboard.set_loop(asyncio.get_running_loop())` before `dashboard.start()`.

2. Rejected list not updating / rejection flow inconsistent
- Cause: latest-approval-state Flux query in Influx was incorrect after pivot.
- Fix: group by `agent_id`, sort desc by `_time`, then `limit(n:1)` to derive latest state per agent.

3. Agents stuck in `INFECTED` (never healed)
- Cause: some demo infection states could persist if sentinel anomaly detection path was bypassed.
- Fix: sentinel now treats `agent.infected == True` as authoritative and forces fallback infection report into containment/healing path.

4. Baseline appears already learned immediately after restart
- Cause: historical data from previous runs.
- Fix: all Influx writes/queries are filtered by run-specific `run_id`.

5. Port `8090` already in use
- Fix:
```bash
lsof -i :8090 | tail -n +2 | awk '{print $2}' | xargs -I{} kill -9 {}
```

6. Stats mismatch between “infected” number and cards
- Fix: stats now expose/use `current_infected` for live count; `total_infections` remains cumulative detected count.

---

## Incident Checklist
When system behavior looks wrong, run this checklist in order:

1. Infra up
- `docker ps` shows `immune-influxdb` and `immune-otel-collector` as running.
- `curl http://localhost:8086/health` returns `status: pass`.

2. App mode
- Confirm Influx env vars are set in startup command.
- Confirm dashboard reachable on `http://localhost:8090`.

3. API sanity
- `/api/status` -> `running=true`
- `/api/agents` returns 10 agents
- `/api/stats` has nonzero `total_executions`

4. Baseline progression
- Around ~15 samples/agent, `has_baseline` should become true in `/api/agents`.

5. Approval flow
- Check `/api/pending-approvals` for severe cases.
- Reject one case, then verify it appears in `/api/rejected-approvals`.
- Use Heal now and verify transition out of `quarantined`.

6. Infected/quarantine consistency
- If cards show prolonged `INFECTED`, check whether agent is entering quarantine.
- Verify recent healing actions in UI and `/api/healings`.

7. Common remediation
- Restart app process.
- If state appears stale, restart demo (new `run_id` isolates state).
- Ensure only one app instance is bound to `:8090`.

---

## Useful Dev Commands
Syntax check core files:
```bash
python3 -m py_compile main.py demo.py orchestrator.py telemetry.py baseline.py memory.py web_dashboard.py influx_store.py
```

Find process on dashboard port:
```bash
lsof -i :8090
```
