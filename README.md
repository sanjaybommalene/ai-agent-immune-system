# 🛡️ AI Agent Immune System

## Concept

Treat AI agents as *living entities* that can get "sick" and require an immune system — not just monitoring.

Traditional observability (logs, metrics, alerts) tells us *when something breaks*. This system:
- **Learns** what normal behavior looks like for each agent
- **Detects** abnormal behavior early (prompt drift, tool loops, token explosions)
- **Quarantines** unhealthy agents to prevent cascading failures
- **Heals** them automatically with progressive actions
- **Learns** which healing actions work over time (adaptive immunity)

## Key Innovation: Healing Memory

Unlike traditional self-healing systems that retry the same fix, our immune system **learns**:
- Remembers which healing actions were attempted
- Never repeats failed cures for the same diagnosis
- Escalates through healing ladder progressively
- Builds knowledge of what works across the system

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      AGENT RUNTIME                          │
│  15+ AI Agents executing tasks, emitting telemetry         │
└─────────────────────────────────────────────────────────────┘
                            ↓ vitals
┌─────────────────────────────────────────────────────────────┐
│                    IMMUNE SYSTEM                            │
│                                                             │
│  ┌─────────────┐   ┌──────────────┐   ┌────────────────┐  │
│  │  Sentinel   │   │ Diagnostician│   │    Healer      │  │
│  │  (Detect)   │ → │  (Diagnose)  │ → │   (Recover)    │  │
│  └─────────────┘   └──────────────┘   └────────────────┘  │
│         ↓                                      ↓            │
│  ┌─────────────────┐              ┌──────────────────────┐ │
│  │   Quarantine    │              │   Immune Memory      │ │
│  │   (Contain)     │              │   (Learn)            │ │
│  └─────────────────┘              └──────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Components

- **agents.py** - Agent runtime with telemetry emission
- **telemetry.py** - Collect and store agent vitals
- **baseline.py** - Learn normal behavior per agent
- **detection.py** - Sentinel that detects anomalies
- **diagnosis.py** - Root cause analysis
- **healing.py** - Recovery actions + healing policies
- **memory.py** - Immune memory (learns what works)
- **quarantine.py** - Isolation controller
- **chaos.py** - Controlled failure injection for demo
- **orchestrator.py** - Main control loop
- **main.py** - Entry point

## Setup

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies (Flask for web dashboard)
pip install -r requirements.txt

# Run the immune system with web dashboard
python main.py
```

**Web Dashboard:** Once running, open http://localhost:8090 in your browser

## Demo Flow

**Minutes 0-1:** Agents run normally, establishing baselines

**Minutes 1-2:** Baselines learned for all agents
- Console shows: "📊 Baseline learned for Agent-X"

**Minute 2:** Chaos injection - simulate failures
- Token spikes, tool loops injected into 2-3 agents

**Minute 2-3:** Sentinel detects infections
- "🚨 INFECTION DETECTED: Agent-3 (token spike: 5800 vs baseline 1200)"
- Agents quarantined immediately

**Minute 3-4:** Healing with learning
- System diagnoses root cause
- Applies first healing action from policy
- If fails: escalates to next action
- Records results in immune memory

**Minute 4+:** Shows learned immunity
- Re-injection triggers healing
- System skips previously failed actions
- Goes straight to what works

## Healing Policies

System uses **healing policy abstraction** - diagnosis types map to ordered action ladders:

```python
HEALING_POLICIES = {
    PROMPT_DRIFT: [RESET_MEMORY → ROLLBACK_PROMPT → REDUCE_AUTONOMY → CLONE_AGENT],
    INFINITE_LOOP: [REDUCE_AUTONOMY → RESET_MEMORY → CLONE_AGENT],
    TOOL_INSTABILITY: [REDUCE_AUTONOMY → ROLLBACK_PROMPT → CLONE_AGENT],
}
```

Immune memory filters the policy ladder, skipping known failures.

## Key Features

✅ **Agent-specific baselines** (not global thresholds)
✅ **Multi-signal anomaly detection** (tokens, latency, tool calls)
✅ **Immediate quarantine** (prevent cascade)
✅ **Progressive healing** (escalate, don't retry)
✅ **Adaptive learning** (never repeat failed cures)
✅ **System-level patterns** (learn what works globally)

## What Makes This Different

| Traditional Monitoring | AI Agent Immune System |
|----------------------|------------------------|
| Service-centric | Agent-centric |
| Static thresholds | Behavioral baselines |
| Alert humans | Autonomous response |
| Reactive | Preventive |
| No learning | Adaptive immunity |

## Future Enhancements

- LLM-assisted diagnosis
- Cross-agent infection spread detection
- Predictive health scoring
- Custom healing action plugins
- Multi-cluster immune coordination
- Web dashboard for visualization

## Why This Matters

As agent systems scale to hundreds or thousands of agents:
- Manual intervention doesn't scale
- Small failures cascade rapidly
- Agent behavior drifts over time

The immune system enables **stability at scale** with autonomous healing and learning.

---

Built for [Hackathon Name] - [Date]
