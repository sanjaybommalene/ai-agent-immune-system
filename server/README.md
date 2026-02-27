# Server API (InfluxDB-backed)

REST bridge between `ApiStore` clients and InfluxDB.
Implements every endpoint from [DOCS §6](../docs/DOCS.md#6-server-rest-api-contract).

## Quick start (recommended)

Use the startup scripts from the project root:

```bash
# Server mode — docker compose brings up InfluxDB + OTEL + Server API,
# then runs the client locally with ApiStore
./start-server.sh

# Local mode — docker compose brings up InfluxDB + OTEL only,
# then runs the client locally with InfluxStore (direct)
./start-local.sh
```

Both scripts source `.env` automatically — copy `.env.example` to `.env` and fill in `INFLUXDB_TOKEN`.

## Manual start

```bash
# 1. Start InfluxDB + OTEL (no server)
docker compose up -d influxdb otel-collector

# 2. Start the server
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=<your-token> \
INFLUXDB_ORG=appd \
INFLUXDB_BUCKET=immune_system \
python server/app.py

# 3. In another terminal, run the client via ApiStore
SERVER_API_BASE_URL=http://localhost:5000 python main.py
```

## Docker compose (full stack)

The root `docker-compose.yml` brings up all three services together:

```bash
INFLUXDB_TOKEN=<your-token> docker compose up -d
# → immune-influxdb   :8086
# → immune-otel-collector :4317/:4318
# → immune-server     :5000
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `INFLUXDB_URL` | `http://localhost:8086` | InfluxDB endpoint |
| `INFLUXDB_TOKEN` | *(required)* | InfluxDB auth token |
| `INFLUXDB_ORG` | `appd` | InfluxDB organization |
| `INFLUXDB_BUCKET` | `immune_system` | InfluxDB bucket |
| `SERVER_API_KEY` | *(empty — auth disabled)* | If set, requires `X-API-Key` or `Authorization: Bearer` header |
| `SERVER_PORT` | `5000` | Port the server listens on |

## Architecture

```
ApiStore client  ──HTTP──▶  server/app.py (Flask :5000)  ──InfluxDB client──▶  InfluxDB :8086
```

The server reuses `immune_system/influx_store.py` directly — no duplicated InfluxDB query logic.
Each request's `X-Run-Id` header scopes data to the correct run.
