"""
Quick demo of AI Agent Immune System (30 seconds) with Web Dashboard
"""
import asyncio
import sys
from agents import create_agent_pool
from orchestrator import ImmuneSystemOrchestrator
from web_dashboard import WebDashboard


async def main():
    """Run a quick 30-second demo with web dashboard"""
    print("Starting AI Agent Immune System Demo...", flush=True)
    
    # Create pool of 10 agents
    agents = create_agent_pool(10)
    print(f"Created {len(agents)} agents", flush=True)
    
    # Create immune system orchestrator
    orchestrator = ImmuneSystemOrchestrator(agents)
    
    # Start web dashboard
    dashboard = WebDashboard(orchestrator, port=8090)
    dashboard.start()
    
    # Run immune system (30 seconds)
    await orchestrator.run(duration_seconds=30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nDemo interrupted by user", flush=True)
        sys.exit(0)
