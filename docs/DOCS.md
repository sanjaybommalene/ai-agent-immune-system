# AI Agent Immune System — Documentation (Single Reference)

This document is the **single reference** for the AI Agent Immune System: architecture, deployment, server API, healing and deviation, internal design (HLD — High-Level Design / LLD — Low-Level Design), and operations. Keep this doc as the source of truth; update it when anything changes.

> **Viewing diagrams:** This doc uses [Mermaid](https://mermaid.js.org/) diagrams. They render natively on **GitHub**. In **VS Code / Cursor**, install the [Markdown Preview Mermaid Support](https://marketplace.visualstudio.com/items?itemName=bierner.markdown-mermaid) extension (`bierner.markdown-mermaid`), then preview with `Cmd+Shift+V`.

---

## 1. Executive Summary

| Layer | Role | Components |
|-------|------|-------------|
| **Client** | Apps, AI agents, and **immune system** | AI agents, full immune system (orchestrator, sentinel, healer, dashboard), **CacheManager** (local restart resilience), **ApiStore** (calls server REST API; no direct DB) |
| **Server** | API + persistence | **Server app** (REST API only) + **InfluxDB**; no immune-system logic |
| **Optional** | Visibility | AppDynamics Controller for agent OpenTelemetry (OTEL) metrics and/or immune-system events |

- **Self-healing:** When **max_deviation** (σ = standard deviations from normal baseline) is **below** `DEVIATION_REQUIRING_APPROVAL` (default 5.0σ), the system **auto-heals**. Above that threshold → **human-in-the-loop** (pending approval in the dashboard). The UI displays deviation in σ (e.g. 5.2σ).
- **Deviation** is calculated in **one place:** **Sentinel** (`immune_system/detection.py`), in `detect_infection(recent_vitals, baseline)`. Baselines are maintained via **EWMA** (Exponential Weighted Moving Average) and cached locally for restart resilience.
- **Restart resilience:** **CacheManager** (`~/.immune_cache/state.json`) persists EWMA baselines, quarantine state, `run_id`, and API key across restarts — no cold-start warmup delay or state loss.

**Alternative deployment:** Server-side — immune system as separate services (telemetry, baseline, sentinel, healer) on Kubernetes/OpenShift with a central DB. This doc focuses on the **recommended** client-deployed model.

---

## 2. Architecture (Client-Deployed)

### 2.1 Topology overview

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#e8eaf6', 'primaryTextColor': '#1a1a1a', 'primaryBorderColor': '#3949ab', 'lineColor': '#37474f', 'secondaryColor': '#f3e5f5', 'tertiaryColor': '#e8f5e9', 'edgeLabelBackground': '#ffffff', 'clusterBkg': '#f5f5f5', 'clusterBorder': '#90a4ae'}}}%%
flowchart TB
    subgraph client ["CLIENT SIDE (Customer environment)"]
        Agents["AI Agents<br/>Emit vitals per task"]

        subgraph immune ["Immune System"]
            Orch[Orchestrator]
            Tel[Telemetry]
            BL[Baseline]
            BL <-.-> Cache["CacheManager"]
            Orch --> Sen[Sentinel]
            Sen --> Diag[Diagnostician] --> Heal[Healer]
            Sen --> Q[Quarantine]
            Q <-.-> Cache
            Heal --> Mem[Memory]
            Dash[Dashboard]
        end

        AS[ApiStore]
    end

    subgraph server ["SERVER SIDE (Your backend)"]
        API["Server App<br/>REST API · Auth"]
        DB[("InfluxDB")]
    end

    Agents --> Orch
    Orch --> Tel
    Orch --> BL
    Tel & BL & Mem -->|write/read| AS
    Dash -->|"Approve / Reject"| Orch
    AS -->|HTTPS| API
    API --> DB

    style client fill:#f0f7ff,stroke:#4a86c8,color:#1a1a1a
    style immune fill:#e8f5e9,stroke:#66bb6a,color:#1a1a1a
    style server fill:#f3e5f5,stroke:#ab47bc,color:#1a1a1a
```

- **Client:** AI agents + full immune system; store is **ApiStore** (HTTP to server only). No InfluxDB client on the client.
- **Server:** One application exposing the REST API (§6); it reads/writes InfluxDB (same schema as `InfluxStore`). No immune-system business logic (no sentinel, healer, or dashboard).
- **Backend:** InfluxDB only; all access via server API.

### 2.2 Client-side components (what runs at the customer)

| Component | Responsibility |
|-----------|-----------------|
| **AI agents** | Execute tasks; produce **vitals** — health metrics per task, analogous to a patient's vital signs (latency_ms, input_tokens, output_tokens, token_count, tool_calls, retries, success, cost, model, error_type, prompt_hash). Each agent has a stable `agent_id` (and optional agent_type, model, mcp_servers — MCP = Model Context Protocol, a standard for tool/resource access). |
| **Orchestrator** | Runs agent loops, sentinel loop, baseline learning; coordinates all components; uses **store** for persistence. |
| **TelemetryCollector** | Records vitals via `store.write_agent_vitals()`; reads recent vitals via `store.get_recent_agent_vitals()`, etc. |
| **BaselineLearner** | EWMA adaptive baselines per metric per agent. Continuously updates on each vitals sample. State cached locally (CacheManager) and periodically persisted to store. |
| **Sentinel** | **Deviation calculated here.** Compares recent vitals to EWMA baseline in `detect_infection(recent, baseline)`; emits InfectionReport (max_deviation, anomalies). Uses a stddev floor (5% of mean) when stddev is 0 — this prevents metrics that were constant during learning from being invisible to detection. |
| **Diagnostician** | Maps anomaly patterns to diagnosis types (prompt_drift, prompt_injection, infinite_loop, tool_instability, cost_overrun, etc.). |
| **Healer** | Applies policy ladder + immune memory; records outcomes via store; triggers quarantine release. Actions: reset_memory, rollback_prompt, reduce_autonomy, revoke_tools, reset_agent. |
| **QuarantineController** | In-memory set of quarantined agent IDs; persisted to CacheManager for restart resilience; approval workflow state (pending/rejected) read/written via store. |
| **ImmuneMemory** | Failed actions and pattern summary via store. |
| **Web Dashboard** | Serves UI; reads state from orchestrator (which reads from store); POST approve/reject/heal-now. |
| **CacheManager** | Local JSON file cache (`~/.immune_cache/state.json`). Persists run_id, EWMA baselines, quarantine set, and API key across restarts. Atomic writes (temp + rename). Periodic flush (30s). File permissions restricted to owner (0600). |
| **Store** | **ApiStore** when `SERVER_API_BASE_URL` is set: same interface as `InfluxStore`, but every call is HTTP to the server REST API. No direct InfluxDB. |

Entry point: `main.py` or `demo.py` with `SERVER_API_BASE_URL` (and optional `SERVER_API_KEY`, `SERVER_RUN_ID`) → orchestrator gets ApiStore; otherwise `INFLUXDB_*` → InfluxStore, or in-memory.

### 2.3 Server-side (API layer + InfluxDB)

- **Server app:** Exposes REST endpoints (§6); authenticates requests (e.g. API key, `X-Run-Id`); for each request runs the corresponding InfluxDB read/write (same logic as `InfluxStore`). No detection, healing, or baseline logic.
- **InfluxDB:** Same schema as current `InfluxStore`: measurements and fields for agent_vitals, baseline_profile, infection_event, quarantine_event, approval_event, healing_event, action_log; tag `run_id` for multi-tenant/run isolation.

### 2.4 Data flow (client-deployed)

0. **Startup:** `main.py` loads CacheManager → restores `run_id`, EWMA baselines, quarantine set, and API key. If cache is missing or corrupt, starts fresh (InfluxDB is the fallback source of truth).
1. **Vitals:** Each agent run → TelemetryCollector.record(vitals) → ApiStore → `POST /api/v1/vitals` → server writes to InfluxDB.
2. **Baseline:** Orchestrator (agent loop) passes each vitals sample to BaselineLearner.update() → EWMA state updated in cache; periodically persisted to store via store.write_baseline_profile().
3. **Detection:** Sentinel loop: for each agent, get recent vitals (store GET → server → InfluxDB) and baseline (from EWMA cache); call **Sentinel.detect_infection(recent, baseline)** → deviation computed here; if infection, quarantine (persisted to cache) and either add to pending (max_deviation ≥ threshold) or schedule heal_agent (max_deviation < threshold).
4. **Approval / healing:** Dashboard reads pending/rejected and healing log via orchestrator (store GETs). User Approve/Reject/Heal-now → orchestrator updates store (POST) → server → InfluxDB. Healer runs on client and writes healing events via store.

### 2.5 Component-to-file mapping (codebase)

| Component | File | Notes |
|-----------|------|--------|
| Entry point | `main.py`, `demo.py` | Store selection (ApiStore vs InfluxStore vs in-memory), orchestrator + dashboard. |
| Orchestrator | `immune_system/orchestrator.py` | Agent loop, sentinel loop, approval/heal-now, store usage. |
| Agents | `immune_system/agents.py` | BaseAgent, AgentState, vitals emission, infection simulation. |
| Telemetry | `immune_system/telemetry.py` | TelemetryCollector; record, get_recent, get_all, get_latest. |
| Baseline | `immune_system/baseline.py` | EWMA BaselineLearner, BaselineProfile; update(), get_baseline; cached + store-persisted. |
| Sentinel | `immune_system/detection.py` | **Deviation** (max_deviation) in detect_infection(recent_vitals, baseline). stddev floor for constant metrics. |
| Cache | `immune_system/cache.py` | CacheManager; local state persistence (run_id, baselines, quarantine, API key). |
| Diagnosis | `immune_system/diagnosis.py` | Diagnostician; anomaly pattern → diagnosis type. |
| Healing | `immune_system/healing.py` | Healer, HEALING_POLICIES, healing actions. |
| Memory | `immune_system/memory.py` | ImmuneMemory; failed actions, pattern summary. |
| Quarantine | `immune_system/quarantine.py` | QuarantineController; quarantine/release set. |
| Dashboard | `immune_system/web_dashboard.py` | Flask REST + UI; approve/reject/heal-now. |
| Store (direct DB) | `immune_system/influx_store.py` | InfluxStore when using InfluxDB directly. |
| Store (API) | `immune_system/api_store.py` | ApiStore when using server REST API. |
| Chaos | `immune_system/chaos.py` | ChaosInjector; optional demo infection injection. |
| Python SDK | `immune_sdk.py` | Lightweight reporter for external Python agents (wraps HTTP ingest). |
| Start script | `start.sh` | Sets env vars and launches `main.py`. |

### 2.6 Agent–immune system communication (client-side)

How agents and the immune system communicate. The system supports **two modes simultaneously**: simulated agents running in-process (for demos) and real external agents reporting via HTTP or the Python SDK.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#e8eaf6', 'primaryTextColor': '#1a1a1a', 'primaryBorderColor': '#3949ab', 'lineColor': '#37474f', 'secondaryColor': '#f3e5f5', 'tertiaryColor': '#e8f5e9', 'edgeLabelBackground': '#ffffff', 'clusterBkg': '#f5f5f5', 'clusterBorder': '#90a4ae'}}}%%
flowchart TB
    subgraph ext ["Real AI Agents"]
        direction LR
        Py["Python Agent<br/>(immune_sdk)"]
        Other["Any Language<br/>(HTTP POST)"]
    end

    subgraph sim ["Simulation"]
        SimA["Simulated Agents"]
    end

    Py -->|SDK| Ingest["POST /api/v1/ingest"]
    Other -->|HTTP JSON| Ingest
    Ingest --> Tel[TelemetryCollector]

    SimA -->|in-process| Orch[Orchestrator]
    Orch --> Tel
    Tel -->|write_agent_vitals| Store[("Store<br/>(ApiStore / InfluxStore)")]

    Orch --> BL[BaselineLearner]
    BL <-.->|EWMA state| Cache["CacheManager"]
    Orch --> Sen[Sentinel]
    Sen --> Diag[Diagnostician]
    Diag --> Heal[Healer]
    Orch <-.->|quarantine / run_id| Cache
    Dash[Web Dashboard] -->|"Approve / Reject"| Orch
```

**Mode 1 — Simulated agents (in-process, for demos):**
- The **orchestrator** owns the asyncio loop and drives both agents and the immune system.
- Each tick, for each agent: `vitals = await agent.execute()` then `self.telemetry.record(vitals)`.
- Communication is **synchronous in-process**: orchestrator pulls vitals from agents and pushes them into `TelemetryCollector`, which either keeps them in memory or writes to the store (InfluxDB or server API).
- The immune system never calls into the agent except for healing (e.g. `agent.state.reset_memory()`). Agents do not call the immune system; the orchestrator is the only bridge.

**Mode 2 — Real external agents (HTTP ingest + Python SDK):**

Real agents report vitals via the implemented HTTP ingest endpoint or the Python SDK. Both paths write into the same `TelemetryCollector` and flow through the same baseline/detection/healing pipeline.

- **HTTP API (`POST /api/v1/ingest`):** Any agent in any language POSTs a JSON payload with vitals fields (`agent_id`, `input_tokens`, `output_tokens`, `latency_ms`, `tool_calls`, `cost`, `model`, `error_type`, `prompt_hash`, etc.). Unknown agents are auto-registered. Endpoint is on the dashboard Flask server (port 8090).
- **Python SDK (`immune_sdk.py`):** Python agents import `ImmuneReporter` and call `reporter.report(...)` after each LLM call. The SDK wraps `POST /api/v1/ingest` and handles auto-registration.
- **Agent registration (`POST /api/v1/agents/register`):** Optional explicit registration with `agent_id`, `agent_type`, and `model`. The ingest endpoint auto-registers on first vitals report if the agent is unknown.

**Vitals fields (production):**

| Field | Source | Description |
|-------|--------|-------------|
| `input_tokens` | LLM response `usage.prompt_tokens` | Prompt / input tokens |
| `output_tokens` | LLM response `usage.completion_tokens` | Completion / output tokens |
| `token_count` | `input_tokens + output_tokens` | Total tokens (backward compat) |
| `latency_ms` | Wall-clock measurement | End-to-end agent turn latency |
| `tool_calls` | Agent framework | Number of tool/function calls |
| `retries` | Agent retry logic | Number of retry attempts |
| `cost` | Calculated from tokens × model price | Estimated cost in USD |
| `model` | Agent config | LLM model used (e.g. `gpt-4o`) |
| `error_type` | Agent error handling | Error category (`rate_limit`, `timeout`, `content_filter`, or empty) |
| `prompt_hash` | Hash of system prompt | Detects prompt drift / injection |
| `success` | Agent outcome | Whether the task completed |

**Other integration patterns (not yet implemented):**
- **Pull by immune system:** Immune system reads vitals from a **shared store** (e.g. InfluxDB or server API) that agents write to independently.
- **OTEL / tracing:** Agents emit OTEL metrics/spans; the immune system reads from the same backend the collector writes to.

---

## 3. Internal Design (HLD / LLD / Data Flow)

The system runs as an asynchronous control plane (it monitors and manages agents without blocking their execution). Persistence can be **InfluxDB-backed** (run-scoped via `run_id`) or **server API–backed** (ApiStore when `SERVER_API_BASE_URL` is set).

### 3.1 HLD (High-Level Design)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#e8eaf6', 'primaryTextColor': '#1a1a1a', 'primaryBorderColor': '#3949ab', 'lineColor': '#37474f', 'secondaryColor': '#f3e5f5', 'tertiaryColor': '#e8f5e9', 'edgeLabelBackground': '#ffffff'}}}%%
flowchart TB
    A["Agent Sources"] --> B[Orchestrator]
    B --> C[Web Dashboard]
    C -->|"Approve / Reject"| B
    B --> D[("Store<br/>(ApiStore / InfluxStore)")]
    B --> Cache["CacheManager<br/>(local JSON)"]
    B --> E[OTel SDK] --> F[OTel Collector]
    B --> G["Telemetry · Baseline · Sentinel<br/>Diagnostician · Healer<br/>Memory · QuarantineCtrl"]
```

### 3.2 LLD (Low-Level Design)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#e8eaf6', 'primaryTextColor': '#1a1a1a', 'primaryBorderColor': '#3949ab', 'lineColor': '#37474f', 'secondaryColor': '#f3e5f5', 'tertiaryColor': '#e8f5e9', 'edgeLabelBackground': '#ffffff'}}}%%
flowchart TD
    M["main.py / demo.py"] --> O[Orchestrator]
    M --> W[WebDashboard]
    W -->|"REST + set_loop"| O

    O --> TC[TelemetryCollector]
    O --> BL[BaselineLearner]
    O --> S[Sentinel]
    O --> D[Diagnostician]
    O --> H[Healer]
    O --> IM[ImmuneMemory]
    O --> Q[QuarantineCtrl]
    O --> CH[ChaosInjector]

    BL <-.->|EWMA state| C["CacheManager"]
    Q <-.->|quarantine set| C
    M -.->|"run_id / api_key"| C

    TC & BL & IM & O --> IS[("Store<br/>(InfluxStore / ApiStore)")]
```

*Note: Store may be InfluxStore or ApiStore; when ApiStore, all DB operations go via HTTP to the server.*

### 3.3 Data flow (sequence)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'actorTextColor': '#1a1a1a', 'actorBkg': '#e8eaf6', 'actorBorder': '#3949ab', 'actorLineColor': '#37474f', 'signalColor': '#37474f', 'signalTextColor': '#1a1a1a', 'labelTextColor': '#1a1a1a', 'loopTextColor': '#1a1a1a', 'noteBkgColor': '#fff9c4', 'noteTextColor': '#1a1a1a', 'noteBorderColor': '#f9a825', 'activationBkgColor': '#e8eaf6', 'activationBorderColor': '#3949ab'}}}%%
sequenceDiagram
    participant A as Agent
    participant O as Orchestrator
    participant T as Telemetry
    participant C as Cache
    participant S as Store
    participant D as Sentinel
    participant U as Dashboard
    participant H as Healer

    rect rgb(230, 245, 255)
    note over A, H: STARTUP
    O->>C: load state.json
    C-->>O: run_id, baselines, quarantine, api_key
    end

    rect rgb(230, 245, 255)
    note over A, H: AGENT LOOP (1s per agent)
    A->>O: vitals
    O->>T: record(vitals)
    T->>S: write vitals
    O->>T: get_count
    O->>C: update EWMA baseline
    O->>S: write baseline
    end

    rect rgb(255, 243, 224)
    note over A, H: SENTINEL LOOP (1s)
    O->>T: get_recent(10s)
    T->>S: query vitals
    O->>D: detect_infection()

    alt infection detected
        O->>S: write infection + quarantine
        O->>C: add_quarantine(agent_id)

        alt severe (≥ threshold)
            O->>S: approval = pending
            U->>O: approve / reject
            alt approved
                O->>H: heal_agent()
                H->>S: write healing event
                H-->>O: result
                O->>S: release quarantine
            else rejected
                O->>S: approval = rejected
            end

        else mild (auto-heal)
            O->>H: heal_agent()
            H->>S: write healing event
            H-->>O: result
            O->>S: release quarantine
        end
    end
    end

    rect rgb(232, 245, 233)
    note over A, H: DASHBOARD READS
    U->>O: GET stats / agents / pending
    O->>S: query state
    end
```

### 3.4 Component mapping (file-level)

- `main.py`, `demo.py` — Entry points at repo root; import from `immune_system` package.
- `immune_system/` — Python package containing all core modules.
- `immune_system/orchestrator.py` — Core event loops: agent execution, sentinel detection, healing, approval/rejection workflow.
- `immune_system/agents.py` — Simulated agents and infection modes; emits vitals each execution.
- `immune_system/telemetry.py` — Telemetry abstraction and OTel metric instruments.
- `immune_system/baseline.py` — Baseline profile learning and retrieval.
- `immune_system/detection.py` — Statistical anomaly detection and deviation scoring (deviation calculated here).
- `immune_system/diagnosis.py` — Rule-based diagnosis from anomaly patterns.
- `immune_system/healing.py` — Healing policies and action execution/validation.
- `immune_system/memory.py` — Immune memory (failed actions/pattern summaries), backed by store queries.
- `immune_system/web_dashboard.py` — REST API + UI rendering; user actions for approval/rejection/heal-now.
- `immune_system/influx_store.py` — InfluxDB persistence/query layer (used when `INFLUXDB_*` is set).
- `immune_system/api_store.py` — Server API–backed store (used when `SERVER_API_BASE_URL` is set). See §6 for API contract.
- `immune_system/logging_config.py` — Logging setup (JSON or colored console).
- `immune_system/quarantine.py`, `immune_system/chaos.py` — Quarantine controller and chaos injector.
- `immune_sdk.py` — Lightweight Python SDK for external agents (wraps `POST /api/v1/ingest`).
- `start.sh` — Convenience script that sets env vars and launches `main.py`.
- `observability/docker-compose.yml` — Local InfluxDB and OTel Collector stack.
- `observability/otel-collector-config.yaml` — OTLP receiver + debug exporter pipeline.

---

## 4. Healing and Deviation (Single Place to Update)

### 4.1 Where deviation is calculated

**Only in:** `immune_system/detection.py` → **`Sentinel.detect_infection(recent_vitals, baseline)`**

- **Inputs:** `recent_vitals` (e.g. last 10s from store/InfluxDB), `baseline` (EWMA mean/stddev per metric, cached locally and backed by store).
- **Logic:** For each metric (latency, tokens, cost, tools, retry rate, error rate), computes: how far is the recent average from the learned normal? Formally: `deviation = |recent_avg − baseline_mean| / stddev`. If this deviation exceeds `threshold_stddev` (default 2.5σ), it's flagged as an anomaly. Also checks whether the system prompt hash changed vs baseline (prompt drift/injection detection).
- **Stddev floor:** When baseline stddev is 0 (constant metric during learning), a floor of 5% of the mean is used so that any change is detectable.
- **Retry/error rates:** Now use consistent stddev-based deviation (no more `× 10` multiplier).
- **Anomaly types:** `token_spike`, `latency_spike`, `tool_explosion`, `high_retry_rate`, `input_token_spike`, `output_token_spike`, `cost_spike`, `prompt_change`, `error_rate_spike`.
- **Output:** `InfectionReport` (max_deviation in σ, anomalies, deviations).

Orchestrator feeds each vitals sample into the EWMA baseline learner and calls `sentinel.detect_infection(recent, baseline)` for each agent on the sentinel loop.

### 4.2 Self-heal vs human-in-the-loop

| Threshold | Behavior |
|----------|----------|
| **< DEVIATION_REQUIRING_APPROVAL** (default **5.0σ**) | **Self-healing:** Orchestrator schedules `heal_agent()`; healer runs policy ladder + immune memory. |
| **≥ 5.0σ** | **HITL (Human-In-The-Loop):** Quarantine + add to pending approvals; healing only after Approve (or Heal now for rejected). |

- **Config:** `immune_system/orchestrator.py`: `DEVIATION_REQUIRING_APPROVAL = 5.0`. Detection sensitivity: `Sentinel(threshold_stddev=2.5)` in `immune_system/detection.py`.

### 4.3 Agent state and healing actions

- **reset_memory** → `agent.state.reset_memory()`  
- **rollback_prompt** → `agent.state.rollback_prompt()`  
- **reduce_autonomy** → `agent.state.reduce_autonomy()`  
- **revoke_tools** → `agent.state.revoke_tools()` (disables tool access; used for prompt injection and infinite loop)  
- **reset_agent** → resets all agent state to defaults.  
Status: `HEALTHY`, `INFECTED`, `QUARANTINED`; after successful healing, release from quarantine.

### 4.4 EWMA adaptive baselines

Baselines use **Exponential Weighted Moving Average** (EWMA) instead of a fixed "first N samples" approach. In plain terms: each new data point slightly adjusts the running average, with recent values weighted more heavily than old ones. This means baselines continuously adapt to natural drift (e.g. an agent gradually using more tokens over weeks) while still flagging sudden anomalies (e.g. tokens doubling in one tick).

```
alpha = 2 / (span + 1)    # span=50 → alpha ≈ 0.039; smaller alpha = slower adaptation
new_mean = alpha × value + (1 − alpha) × old_mean
new_variance = (1 − alpha) × (old_variance + alpha × (value − new_mean)²)
```

- After `min_samples` (15) warmup, the baseline is "ready" and the Sentinel starts using it.
- Baselines are cached locally (`CacheManager`) and periodically persisted to the store (InfluxDB or server API).
- On restart, cached baselines are loaded immediately — no cold-start delay.

### 4.5 Cache layer (restart resilience)

`CacheManager` (`immune_system/cache.py`) maintains a local JSON snapshot at `$IMMUNE_CACHE_DIR/state.json` (default `~/.immune_cache/state.json`):

| Cached state | Purpose |
|-------------|---------|
| `run_id` | Persistent across restarts — same run_id reuses historical data in InfluxDB. |
| `baselines` | EWMA state per agent — no re-learning needed on restart. |
| `quarantine` | Set of quarantined agent IDs — survives process restart. |
| `api_key` | Generated once, reused across restarts. Env var `INGEST_API_KEY` takes precedence when set. |

File is atomic-written (temp + rename) to prevent corruption. Permissions restricted to owner (`0600`). Flushed every 30 s, on shutdown, and immediately after critical state changes (quarantine add/remove, first baseline ready). The cache includes a `_schema_version` field; if the version changes after an upgrade the stale cache is discarded and the system starts fresh.

**Cache directory** is configurable via `IMMUNE_CACHE_DIR` env var. In Docker, mount a volume and point the var at it: `-v /data/immune-cache:/data/immune-cache -e IMMUNE_CACHE_DIR=/data/immune-cache`. In Kubernetes, use a PersistentVolumeClaim.

**Graceful degradation** — if the cache file is lost (e.g. ephemeral container, pod reschedule, blue/green deploy): a new `run_id` is generated, baselines are recovered from the store (InfluxDB/API) or re-learned after ~15 samples, and quarantine state is re-detected by the sentinel within 1–2 seconds.

### 4.6 API authentication

The ingest and registration endpoints (`POST /api/v1/ingest`, `POST /api/v1/agents/register`) require an `X-API-KEY` header. Priority order: env var `INGEST_API_KEY` → cached value → auto-generated on first run and persisted in cache. Only the key prefix is logged on startup (not the full key).

Dashboard UI endpoints (read-only) do not require authentication.

---

## 5. Client Configuration

### 5.1 Store selection (main.py / demo.py)

1. If **`SERVER_API_BASE_URL`** set → **ApiStore** (server API).  
2. Else if **`INFLUXDB_*`** set → **InfluxStore** (direct InfluxDB).  
3. Else → in-memory (no store).

### 5.2 Environment variables (client)

| Variable | Purpose |
|----------|---------|
| `SERVER_API_BASE_URL` | Base URL of server app. When set, use ApiStore. |
| `SERVER_API_KEY` | Optional; header `Authorization: Bearer <key>` or `X-API-Key: <key>`. |
| `SERVER_RUN_ID` | Optional; sent as `X-Run-Id`. |
| `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` | Direct InfluxDB when server API not set. |
| `IMMUNE_CACHE_DIR` | Directory for the local state cache file. Defaults to `~/.immune_cache`. Set to a volume-mounted path in Docker/K8s. |
| `INGEST_API_KEY` | API key for ingest/register endpoints. Auto-generated and cached if not set. |

---

## 6. Server REST API Contract

Run context via header `X-Run-Id` or query `run_id`. Server maps each endpoint to InfluxDB (same schema as `InfluxStore`).

### 6.1 Telemetry

| Method | Path | Request | Response |
|--------|------|---------|----------|
| POST | `/api/v1/vitals` | JSON: agent_id, agent_type?, latency_ms, token_count, input_tokens?, output_tokens?, tool_calls, retries, success, cost?, model?, error_type?, prompt_hash?, timestamp? | 204 |
| GET | `/api/v1/vitals/recent` | Query: agent_id, window_seconds | 200 JSON array |
| GET | `/api/v1/vitals/all` | Query: agent_id | 200 JSON array |
| GET | `/api/v1/vitals/latest` | Query: agent_id | 200 object or 404 |
| GET | `/api/v1/vitals/execution-count` | Query: agent_id | 200 `{ "count": number }` |
| GET | `/api/v1/vitals/total-executions` | - | 200 `{ "total": number }` |

### 6.2 Baselines

| Method | Path | Request | Response |
|--------|------|---------|----------|
| POST | `/api/v1/baselines` | JSON: baseline profile (agent_id, latency_mean/stddev/p95, tokens_mean/stddev/p95, tools_mean/stddev/p95, input_tokens_*, output_tokens_*, cost_*, prompt_hash, sample_size) | 204 |
| GET | `/api/v1/baselines/{agent_id}` | - | 200 or 404 |
| GET | `/api/v1/baselines/count` | - | 200 `{ "count": number }` |

### 6.3 Events

| Method | Path | Request | Response |
|--------|------|---------|----------|
| POST | `/api/v1/events/infection` | JSON: agent_id, max_deviation (σ), anomalies[], deviations, diagnosis_type | 204 |
| POST | `/api/v1/events/quarantine` | JSON: agent_id, action, duration_s? | 204 |

### 6.4 Approvals

| Method | Path | Request | Response |
|--------|------|---------|----------|
| POST | `/api/v1/approvals` | JSON: agent_id, decision, max_deviation?, anomalies?, diagnosis_type?, reasoning?, infection_payload? | 204 |
| GET | `/api/v1/approvals/latest` | Query: optional agent_id | 200 by_agent or single |
| GET | `/api/v1/approvals/pending` | - | 200 array |
| GET | `/api/v1/approvals/rejected` | - | 200 array |

### 6.5 Healing

| Method | Path | Request | Response |
|--------|------|---------|----------|
| POST | `/api/v1/healing/events` | JSON: agent_id, diagnosis_type, healing_action, success, validation_passed, trigger?, message? | 204 |
| GET | `/api/v1/healing/failed-actions` | Query: agent_id, diagnosis_type | 200 `{ "actions": string[] }` |
| GET | `/api/v1/healing/total` | - | 200 `{ "total": number }` |
| GET | `/api/v1/healing/success-rate` | - | 200 `{ "rate": number }` |
| GET | `/api/v1/healing/pattern-summary` | - | 200 object |

### 6.6 Action log

| Method | Path | Request | Response |
|--------|------|---------|----------|
| POST | `/api/v1/action-log` | JSON: action_type, agent_id, payload | 204 |
| GET | `/api/v1/action-log/recent` | Query: limit? (default 50) | 200 array |

### 6.7 Health

| Method | Path | Response |
|--------|------|----------|
| GET | `/api/v1/health` | 200 if server and InfluxDB reachable |
| POST | `/api/v1/run` | Optional: return `{ "run_id": "..." }` |

---

## 7. Backend: InfluxDB Schema

Same as `InfluxStore`: measurements **agent_vitals**, **baseline_profile**, **infection_event**, **quarantine_event**, **approval_event**, **healing_event**, **action_log**; tag **run_id** for isolation. Server app is the only writer/reader when using client-deployed model; client uses API only.

---

## 8. Optional: AppDynamics Integration

AppDynamics is Cisco's application performance monitoring (APM) platform. It can be used alongside the immune system for enterprise-grade observability and alerting.

- **Client** (or server) can push immune-system custom metrics/events (infection, quarantine, healing) to AppDynamics Controller for dashboards/alerting.
- Agent OTEL can be sent via OTEL Collector to Controller separately.  
No change to where deviation is calculated or how healing works.

---

## 9. Alternative: Server-Side Deployment

When the immune system runs as **separate services** (not on the client):

- **Agents:** External (customer environment); emit OTEL to a pipeline (Collector → backend).
- **Immune system:** Deployed as pods/services: Telemetry Ingestion, Baseline Service, Sentinel (detection), Quarantine+Approval, Healer, Dashboard API. Each can scale independently.
- **Backend:** Central DB (e.g. PostgreSQL or InfluxDB) for baselines, quarantine state, approvals, immune memory, action log. OTEL backend (e.g. Prometheus, Tempo, vendor) for metrics/traces.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#e8eaf6', 'primaryTextColor': '#1a1a1a', 'primaryBorderColor': '#3949ab', 'lineColor': '#37474f', 'secondaryColor': '#f3e5f5', 'tertiaryColor': '#e8f5e9', 'edgeLabelBackground': '#ffffff', 'clusterBkg': '#f5f5f5', 'clusterBorder': '#90a4ae'}}}%%
flowchart TB
    Ingress["Ingress (TLS, auth)"]
    Ingress --> Dashboard & Gateway[API Gateway] & Webhooks

    subgraph services ["Immune Services (Pods)"]
        direction LR
        TI[Telemetry] ~~~ BL[Baseline] ~~~ Sen[Sentinel]
        QA[Quarantine] ~~~ Heal[Healer]
    end

    Dashboard & Gateway & Webhooks --> services
    services --> PG[(PostgreSQL)] & Redis["Redis"] & OTEL[OTEL Backend]
```

Same logical components (telemetry, baseline, sentinel, healer, approval); data path is OTEL → ingestion → store, and detection/healing run in your cluster instead of on the client. For the **recommended** client-deployed model, all of that runs on the client and persistence goes through the server API + InfluxDB (§2).

---

## 10. Migration Path

- **Client-deployed:** Phase 1 — server API + InfluxDB, client with ApiStore; Phase 2 — auth, multi-run; Phase 3 — optional AppDynamics; Phase 4 — Agent Lifecycle if needed; Phase 5 — RBAC, audit.
- **Server-side:** Phase 1 — persistence + APIs; Phase 2 — OTEL; Phase 3 — split services; Phase 4 — Agent Lifecycle; Phase 5 — enterprise.

---

## 11. Security, SLOs, Design Decisions

- **Security:** Auth on all APIs (e.g. API key, OIDC — OpenID Connect); RBAC (Role-Based Access Control); audit logging; secrets from vault/K8s; TLS.
- **SLOs (examples):** Detection &lt; 2 min; quarantine &lt; 30s; approval API p99 &lt; 500 ms.
- **Design:** HITL by deviation threshold (5.0σ); immune memory per-agent per-diagnosis; healing via Agent Lifecycle API (platform implements).

---

## 12. Runtime Notes and Tradeoffs

### 12.1 Runtime notes

- Tick interval: 1 second (agent loop and sentinel loop).
- Baseline warmup: ~15 samples per agent (configurable via `BaselineLearner.min_samples`); EWMA continuously adapts after warmup.
- Severe infections require explicit approval.
- Rejected healings remain quarantined until user clicks Heal now.
- Run isolation: all Influx reads/writes are filtered by `run_id` to avoid historical contamination (or `X-Run-Id` when using server API).
- Restart resilience: CacheManager restores baselines, quarantine, and run_id on startup — no cold-start delay.

### 12.2 Current tradeoffs (POC)

- InfluxDB-only workflow state is event-sourced (state is derived by replaying a sequence of events rather than stored as a mutable record) and eventually consistent — sufficient for demo and small-scale use.
- For production-grade strict state transitions, a transactional workflow store can be added later while keeping Influx for telemetry.

---

## 13. Operations Runbook

Operational runbook for local demo (InfluxDB + OpenTelemetry) and production client-deployed mode (server API).

- **Local demo:** Use InfluxDB and OTEL env vars below; run from project root.
- **Production (client-deployed):** Set `SERVER_API_BASE_URL` (and optional `SERVER_API_KEY`, `SERVER_RUN_ID`). See §5 for client configuration.

### 13.1 Prerequisites

- Docker + Docker Compose installed
- Python virtualenv created at `venv/`
- Dependencies installed from `requirements.txt`

### 13.2 Quick Start (2-minute demo)

1. Start observability stack (from project root):
```bash
cd observability
docker compose up -d
```

2. Start demo app (from project root). Replace the `INFLUXDB_TOKEN` with the token you configured in InfluxDB (or copy from your `.env` file — see `.env.example`):
```bash
source venv/bin/activate
./start.sh
# Or manually:
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=<your-influxdb-token> \
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

### 13.3 Quick Start (10-minute run)

From project root:
```bash
source venv/bin/activate
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=<your-influxdb-token> \
INFLUXDB_ORG=appd \
INFLUXDB_BUCKET=immune_system \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_METRIC_EXPORT_INTERVAL_MS=5000 \
OTEL_SERVICE_NAME=ai-agent-immune-system \
RUN_DURATION_SECONDS=600 \
python3 demo.py
```

### 13.4 Main App Run (non-demo)

From project root:
```bash
source venv/bin/activate
INFLUXDB_URL=http://localhost:8086 \
INFLUXDB_TOKEN=<your-influxdb-token> \
INFLUXDB_ORG=appd \
INFLUXDB_BUCKET=immune_system \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_SERVICE_NAME=ai-agent-immune-system \
RUN_DURATION_SECONDS=1200 \
python3 main.py
```

### 13.5 Environment variables (runbook)

For **local demo** (direct InfluxDB). Required for DB-backed mode:
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
- If Influx env vars are missing (and `SERVER_API_BASE_URL` is not set), app falls back to in-memory mode.
- For **server API mode**, set `SERVER_API_BASE_URL` (optional: `SERVER_API_KEY`, `SERVER_RUN_ID`). See §5.
- Each run is isolated by `run_id` in Influx (or via `X-Run-Id` when using server API), so old data does not pollute new runs.

### 13.6 Health checks

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

### 13.7 Shutdown

Stop app:
```bash
pkill -f "python3 demo.py" || true
pkill -f "python3 main.py" || true
```

Stop observability stack (from project root):
```bash
cd observability
docker compose down
```

### 13.8 Known issues and fixes

1. **Approve/Reject actions appear to do nothing in demo**
   - Cause: dashboard loop reference missing.
   - Fix: `demo.py` must call `dashboard.set_loop(asyncio.get_running_loop())` before `dashboard.start()`.

2. **Rejected list not updating / rejection flow inconsistent**
   - Cause: latest-approval-state Flux query in Influx was incorrect after pivot.
   - Fix: group by `agent_id`, sort desc by `_time`, then `limit(n:1)` to derive latest state per agent.

3. **Agents stuck in `INFECTED` (never healed)**
   - Cause: some demo infection states could persist if sentinel anomaly detection path was bypassed.
   - Fix: sentinel now treats `agent.infected == True` as authoritative and forces fallback infection report into containment/healing path.

4. **Baseline appears already learned immediately after restart**
   - Cause: historical data from previous runs.
   - Fix: all Influx writes/queries are filtered by run-specific `run_id`.

5. **Port `8090` already in use**
   - Fix:
   ```bash
   lsof -i :8090 | tail -n +2 | awk '{print $2}' | xargs -I{} kill -9 {}
   ```

6. **Stats mismatch between “infected” number and cards**
   - Fix: stats now expose/use `current_infected` for live count; `total_infections` remains cumulative detected count.

### 13.9 Incident checklist

When system behavior looks wrong, run this checklist in order:

1. **Infra up**
   - `docker ps` shows `immune-influxdb` and `immune-otel-collector` as running.
   - `curl http://localhost:8086/health` returns `status: pass`.

2. **App mode**
   - Confirm Influx env vars are set in startup command (or `SERVER_API_BASE_URL` for server API mode).
   - Confirm dashboard reachable on `http://localhost:8090`.

3. **API sanity**
   - `/api/status` -> `running=true`
   - `/api/agents` returns 10–15 agents (10 for `demo.py`, 15 for `main.py`)
   - `/api/stats` has nonzero `total_executions`

4. **Baseline progression**
   - Around ~15 samples/agent, `has_baseline` should become true in `/api/agents`.

5. **Approval flow**
   - Check `/api/pending-approvals` for severe cases.
   - Reject one case, then verify it appears in `/api/rejected-approvals`.
   - Use Heal now and verify transition out of `quarantined`.

6. **Infected/quarantine consistency**
   - If cards show prolonged `INFECTED`, check whether agent is entering quarantine.
   - Verify recent healing actions in UI and `/api/healings`.

7. **Common remediation**
   - Restart app process.
   - If state appears stale, restart demo (new `run_id` isolates state).
   - Ensure only one app instance is bound to `:8090`.

### 13.10 Useful dev commands

Syntax check core files:
```bash
python3 -m py_compile main.py demo.py immune_system/orchestrator.py immune_system/telemetry.py immune_system/baseline.py immune_system/detection.py immune_system/healing.py immune_system/memory.py immune_system/quarantine.py immune_system/chaos.py immune_system/web_dashboard.py immune_system/influx_store.py immune_system/api_store.py immune_system/cache.py
```

Find process on dashboard port:
```bash
lsof -i :8090
```

---

## 14. One-Page Summary

- **What:** Control plane for AI agents: monitor → detect (deviation in Sentinel) → quarantine → heal (self-heal or HITL by deviation threshold).
- **Where deviation is calculated:** **Sentinel** (`immune_system/detection.py`, `detect_infection()`). Baselines via EWMA (cached locally by CacheManager; backed by store).
- **Restart resilience:** CacheManager persists run_id, EWMA baselines, quarantine, and API key across restarts — no cold-start delay.
- **Recommended production:** **Client-deployed** — immune system on client, server = REST API + InfluxDB, client uses ApiStore.
- **Next steps:** Implement server API (contract in §6) + InfluxDB; run client with `SERVER_API_BASE_URL`.

---

## 15. Doc / code / test sync

Keep this document, the codebase, and the test suite aligned with the **project idea**: a control plane for AI agents that monitors vitals, detects anomalies (Sentinel), quarantines, and heals with policy + immune memory, with optional HITL for severe deviation.

| Check | Where |
|-------|--------|
| **Component → file** | §2.5 table; each row must match a file under `immune_system/` or repo root. |
| **Deviation / healing** | §4: deviation only in `detection.py`; threshold 5.0σ in `orchestrator.py`; healing actions in `healing.py`. |
| **Server API contract** | §6: paths and payloads must match `api_store.py` and any server implementation. |
| **Diagrams** | §2.1, §2.6, §3.1–3.3, §9: Mermaid reflects client-deployed flow, store, and optional server-side. |
| **Tests** | `tests/README.md` maps DOCS scenarios to test files; run `pytest tests/` (use venv: `./venv/bin/python -m pytest tests/`). |

When changing behavior or adding components, update DOCS first, then code, then add or adjust tests and `tests/README.md`.

For production deployment, see **`docs/PRODUCTION_READINESS.md`** (gaps, security, observability, and prioritized recommendations).

---

*Single documentation reference — update this doc when anything changes.*
