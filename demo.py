"""
Quick demo of AI Agent Immune System (30 seconds) with Web Dashboard
"""
import asyncio
import os
import sys
from agents import create_agent_pool
from orchestrator import ImmuneSystemOrchestrator
from web_dashboard import WebDashboard
from influx_store import InfluxStore
from logging_config import setup_logging, get_logger
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


async def main():
    """Run a quick 30-second demo with web dashboard"""
    setup_logging()
    configure_otel()

    logger.info("Starting AI Agent Immune System Demo")
    
    # Create pool of 10 agents
    agents = create_agent_pool(10)
    logger.info("Created %d agents", len(agents))

    store = None
    influx_url = os.getenv("INFLUXDB_URL")
    influx_token = os.getenv("INFLUXDB_TOKEN")
    influx_org = os.getenv("INFLUXDB_ORG")
    influx_bucket = os.getenv("INFLUXDB_BUCKET")
    if influx_url and influx_token and influx_org and influx_bucket:
        store = InfluxStore(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
        )
    
    # Create immune system orchestrator
    orchestrator = ImmuneSystemOrchestrator(agents, store=store)
    
    # Start web dashboard
    dashboard = WebDashboard(orchestrator, port=8090)
    dashboard.set_loop(asyncio.get_running_loop())
    dashboard.start()
    
    duration_seconds = int(os.getenv("RUN_DURATION_SECONDS", "600"))
    await orchestrator.run(duration_seconds=duration_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Demo interrupted by user")
        sys.exit(0)
