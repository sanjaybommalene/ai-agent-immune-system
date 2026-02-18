"""
AI Agent Immune System - Main Entry Point

A system that treats AI agents as living entities with an immune system that:
- Learns normal behavior (EWMA adaptive baselines)
- Detects infections (statistical anomaly detection)
- Quarantines unhealthy agents
- Heals them with progressive actions
- Remembers which healing actions work (adaptive immunity)
- Persists state across restarts via local cache + InfluxDB
"""
import asyncio
import os
import sys
from immune_system.agents import create_agent_pool
from immune_system.orchestrator import ImmuneSystemOrchestrator
from immune_system.web_dashboard import WebDashboard
from immune_system.influx_store import InfluxStore
from immune_system.api_store import ApiStore
from immune_system.cache import CacheManager
from immune_system.logging_config import setup_logging, get_logger
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource

logger = get_logger(__name__)


def configure_otel():
    """Configure OTEL metrics export when OTLP endpoint is available."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "ai-agent-immune-system")
    export_interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "5000"))
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics"),
        export_interval_millis=export_interval_ms,
    )
    provider = MeterProvider(
        metric_readers=[reader],
        resource=Resource.create({"service.name": service_name}),
    )
    metrics.set_meter_provider(provider)


def _check_influx_health(store: InfluxStore) -> bool:
    """Ping InfluxDB on startup; return True if reachable."""
    try:
        health = store.client.health()
        if health.status == "pass":
            logger.info("InfluxDB health check passed")
            return True
        logger.warning("InfluxDB health check returned status=%s", health.status)
        return False
    except Exception as exc:
        logger.warning("InfluxDB unreachable on startup: %s", exc)
        return False


async def main():
    """Main entry point with web dashboard"""
    setup_logging()
    configure_otel()

    logger.info("Starting AI Agent Immune System")

    cache = CacheManager()
    cache.load()

    run_id = cache.get_run_id()
    logger.info("Using run_id=%s (persistent across restarts)", run_id)

    server_api_base = os.getenv("SERVER_API_BASE_URL")
    influx_url = os.getenv("INFLUXDB_URL")
    influx_token = os.getenv("INFLUXDB_TOKEN")
    influx_org = os.getenv("INFLUXDB_ORG")
    influx_bucket = os.getenv("INFLUXDB_BUCKET")

    store = None
    if server_api_base:
        store = ApiStore(
            base_url=server_api_base,
            api_key=os.getenv("SERVER_API_KEY"),
            run_id=os.getenv("SERVER_RUN_ID") or run_id,
        )
        logger.info("Server API store enabled (base_url=%s)", server_api_base)
    elif influx_url and influx_token and influx_org and influx_bucket:
        store = InfluxStore(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            run_id=run_id,
        )
        if not _check_influx_health(store):
            logger.warning("Continuing with InfluxDB store despite failed health check")
        logger.info("InfluxDB enabled (bucket=%s, run_id=%s)", influx_bucket, run_id)
    else:
        logger.warning("Neither SERVER_API_BASE_URL nor InfluxDB env vars set. Falling back to in-memory mode.")

    agents = create_agent_pool(15)
    logger.info("Created %d agents", len(agents))

    orchestrator = ImmuneSystemOrchestrator(agents, store=store, cache=cache)

    api_key = cache.get_api_key()
    dashboard = WebDashboard(orchestrator, port=8090, api_key=api_key)
    dashboard.set_loop(asyncio.get_running_loop())
    dashboard.start()

    logger.info("Ingest API key configured (prefix: %s...)", api_key[:8])

    cache.start_flush_task(asyncio.get_running_loop())

    duration_seconds = int(os.getenv("RUN_DURATION_SECONDS", "1200"))

    try:
        await orchestrator.run(duration_seconds=duration_seconds)
    finally:
        cache.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
        sys.exit(0)
