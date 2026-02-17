# ARCHITECTURE

## Overview
The AI Agent Immune System is an async control plane that monitors agent vitals, learns baselines, detects infections, quarantines unhealthy agents, and heals them with policy-driven actions (plus user approvals for severe cases).

Persistence is InfluxDB-backed for telemetry and workflow state (run-scoped via `run_id`).

---

## HLD (High-Level Design)

```mermaid
flowchart LR
    A[Agent Runtime\n10-15 simulated agents] --> B[Immune System Orchestrator\nAsync control plane]
    B --> C[Web Dashboard\nFlask + REST + UI]
    B --> D[(InfluxDB\nsource of truth for run data)]
    B --> E[OTel SDK Metrics]
    E --> F[OTel Collector\nOTLP receiver]

    subgraph BI[Orchestrator Internal Components]
      T[TelemetryCollector]
      BL[BaselineLearner]
      S[Sentinel]
      DG[Diagnostician]
      H[Healer]
      IM[ImmuneMemory]
      Q[QuarantineController]
      CH[ChaosInjector]
    end

    B --> BI
    C -->|Approve/Reject/Heal-now| B
```

---

## LLD (Low-Level Design)

```mermaid
flowchart TD
    M[main.py / demo.py] --> O[ImmuneSystemOrchestrator]
    M --> W[WebDashboard]
    W -->|set_loop| O

    O --> T[TelemetryCollector]
    O --> BL[BaselineLearner]
    O --> S[Sentinel]
    O --> D[Diagnostician]
    O --> H[Healer]
    O --> IM[ImmuneMemory]
    O --> Q[QuarantineController]
    O --> CH[ChaosInjector]
    O --> IS[InfluxStore]

    T -->|record/get_recent/get_latest|getIS[(Influx measurements)]
    BL -->|save/get baseline|getIS
    IM -->|failed actions/pattern queries|getIS
    O -->|approval/reject state|getIS
    O -->|action log|getIS

    W -->|/api/agents /stats /pending /rejected /healings| O
    W -->|POST approve/reject/heal-now| O
    O -->|run_coroutine_threadsafe| O
```

---

## Data Flow Diagram

```mermaid
sequenceDiagram
    participant Agent as Agent.execute()
    participant Orch as Orchestrator
    participant Tel as TelemetryCollector
    participant Inf as InfluxDB
    participant Sen as Sentinel
    participant UI as Dashboard/UI
    participant Heal as Healer

    loop Every 1s per agent
        Agent->>Orch: vitals dict
        Orch->>Tel: record(vitals)
        Tel->>Inf: write agent_vitals (run_id scoped)
        Orch->>Tel: get_count/get_all
        Orch->>Orch: baseline readiness check
        Orch->>Inf: write/read baseline_profile
    end

    loop Every 1s sentinel
        Orch->>Tel: get_recent(agent, 10s)
        Tel->>Inf: query recent vitals
        Orch->>Sen: detect_infection(recent, baseline)

        alt agent.infected or anomaly detected
            Orch->>Inf: write infection/quarantine events
            alt severity >= threshold
                Orch->>Inf: write approval_event=pending
                UI->>Orch: approve/reject
                alt approved
                    Orch->>Heal: heal_agent(trigger=after_approval)
                else rejected
                    Orch->>Inf: write approval_event=rejected
                end
            else mild
                Orch->>Heal: heal_agent(trigger=auto)
            end
        end

        Heal->>Inf: write healing_event + action_log
        Heal->>Orch: success/fail
        Orch->>Inf: quarantine release event
    end

    UI->>Orch: GET stats/agents/pending/rejected/healings
    Orch->>Inf: query run-scoped state
```

---

## Component Mapping

- `main.py`
  - App entrypoint, OTel configuration, Influx store wiring, run duration control.
- `demo.py`
  - Demo entrypoint, dashboard startup with event loop reference, configurable duration.
- `orchestrator.py`
  - Core event loops: agent execution, sentinel detection, healing, approval/rejection workflow.
- `agents.py`
  - Simulated agents and infection modes; emits vitals each execution.
- `telemetry.py`
  - Telemetry abstraction and OTel metric instruments.
- `baseline.py`
  - Baseline profile learning and retrieval.
- `detection.py`
  - Statistical anomaly detection and severity scoring.
- `diagnosis.py`
  - Rule-based diagnosis from anomaly patterns.
- `healing.py`
  - Healing policies and action execution/validation.
- `memory.py`
  - Immune memory (failed actions/pattern summaries), backed by DB queries.
- `web_dashboard.py`
  - REST API + UI rendering; user actions for approval/rejection/heal-now.
- `influx_store.py`
  - InfluxDB persistence/query layer for telemetry, baselines, approvals, healing events, action logs.
- `observability/docker-compose.yml`
  - Local InfluxDB and OTel Collector stack.
- `observability/otel-collector-config.yaml`
  - OTLP receiver + debug exporter pipeline.

---

## Runtime Notes

- Tick interval: 1 second (agent loop and sentinel loop).
- Baseline warmup: ~15 samples per agent.
- Severe infections require explicit approval.
- Rejected healings remain quarantined until user clicks Heal now.
- Run isolation: all Influx reads/writes are filtered by `run_id` to avoid historical contamination.

---

## Current Tradeoffs (POC)

- InfluxDB-only workflow state is event-sourced and eventually consistent enough for demo scale.
- For production-grade strict state transitions, a transactional workflow store can be added later while keeping Influx for telemetry.
