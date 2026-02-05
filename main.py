"""
AI Agent Immune System - Main Entry Point

A system that treats AI agents as living entities with an immune system that:
- Learns normal behavior
- Detects infections (abnormal behavior)
- Quarantines unhealthy agents
- Heals them with progressive actions
- Remembers which healing actions work (adaptive immunity)
"""
import asyncio
import sys
from agents import create_agent_pool
from orchestrator import ImmuneSystemOrchestrator
from web_dashboard import WebDashboard


async def main():
    """Main entry point with web dashboard"""
    print("Starting AI Agent Immune System...", flush=True)
    
    # Create pool of 15 diverse agents
    agents = create_agent_pool(15)
    print(f"Created {len(agents)} agents", flush=True)
    
    # Create immune system orchestrator
    orchestrator = ImmuneSystemOrchestrator(agents)
    
    # Start web dashboard
    dashboard = WebDashboard(orchestrator, port=8090)
    dashboard.start()
    
    # Run for 2 minutes (120 seconds) - adjust as needed
    await orchestrator.run(duration_seconds=120)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nShutdown requested by user", flush=True)
        sys.exit(0)
