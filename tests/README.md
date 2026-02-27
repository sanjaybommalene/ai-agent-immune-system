# Test Coverage vs Real-World Scenarios

This document maps **DOCS.md** real-world behavior to test coverage and lists **gaps** to address for production confidence.

## What Is Covered Well

| Area | Tests | Real-world scenario |
|------|--------|----------------------|
| **Sentinel** | `test_detection.py` | All anomaly types (latency, token, tool, cost, retry, error, prompt), stddev floor, max deviation |
| **Baseline** | `test_baseline.py` | EWMA warmup, convergence, drift, cache round-trip, multi-agent, prompt_hash |
| **Cache** | `test_cache.py` | Load/save, schema version, atomic write, 0600 permissions, run_id, quarantine, API key, env override, concurrent writes |
| **Telemetry** | `test_telemetry.py` | Record, get_recent (window), get_latest, bounded buffer, multi-agent, token_count from input+output |
| **Diagnosis** | `test_diagnosis.py` | All diagnosis types (prompt_injection, prompt_drift, cost_overrun, infinite_loop, tool_instability, memory_corruption, unknown), confidence |
| **Multi-hypothesis diagnosis** | `test_diagnosis_multi.py` | DiagnosisResult with ranked hypotheses, fleet-wide EXTERNAL_CAUSE, deduplication, backward-compat diagnose_single, operator feedback confidence adjustment |
| **Success-weighted healing** | `test_diagnosis_multi.py` | Default ordering, skip failed actions, reorder by global success patterns, exhaustion returns None, EXTERNAL_CAUSE policy, cross-agent immune memory, success rates, feedback storage |
| **Healing policies** | `test_healing.py` | Policy ladder, next_action (skip failed), all diagnosis types have policies ending in RESET_AGENT |
| **Probation validation** | `test_probation.py` | Healer.validate_probation (no baseline, insufficient data, healthy sentinel, infected sentinel), lifecycle probation states (enter, tick counting, completion, pass-to-healthy, fail-back-to-healing, execution allowed), baseline adaptation (reset, accelerate, deceleration revert, reset-then-relearn) |
| **Lifecycle** | `test_lifecycle.py` | 8-state machine transitions, anomaly escalation (SUSPECTED → DRAINING), draining, healing, probation tick counting, exhausted state, execution blocking, history |
| **Enforcement** | `test_enforcement.py` | NoOpEnforcement, GatewayEnforcement (mock policy engine), ProcessEnforcement (mock PID/signals), ContainerEnforcement (mock Docker/K8s), CompositeEnforcement (chained strategies) |
| **Executor** | `test_executor.py` | SimulatedExecutor (agent state changes), GatewayExecutor (mock policy injection), ProcessExecutor (mock HTTP control API), ContainerExecutor (mock commands and fallback) |
| **Correlator** | `test_correlator.py` | AGENT_SPECIFIC, FLEET_WIDE, PARTIAL_FLEET verdicts, mock Sentinel and TelemetryCollector |
| **Orchestrator (integration)** | `test_orchestrator.py` | Baseline learning, latency spike → infection, quarantine → cache persist/restore, **HITL**: severe → pending, approve, reject; **auto-heal**; deviation threshold |
| **SDK** | `test_sdk.py` | Payload construction, API key header, buffering, error callback; mocks HTTP to ingest |
| **Gateway: Vitals** | `test_gateway_vitals.py` | System prompt extraction, tool-call counting, cost estimation, full vitals from request/response, streaming chunk extraction |
| **Gateway: Fingerprint** | `test_gateway_fingerprint.py` | X-Agent-ID header, API key hash, IP+UA fallback, priority order, agent type derivation from User-Agent |
| **Gateway: Discovery** | `test_gateway_discovery.py` | New agent creation, count increment, model/IP accumulation, type upgrade, callback on new agent, list/count |
| **Gateway: Policy** | `test_gateway_policy.py` | Empty rules allow all, model block/allow lists, rate limiting, glob pattern matching, rule serialization |
| **Gateway: App** | `test_gateway_app.py` | Health endpoint, agents/stats/policies/vitals/baseline APIs, proxy passthrough, Cache-Control headers |
| **Gateway: OTEL** | `test_gateway_otel.py` | LLM span detection heuristics, span-to-vitals extraction, error status, agent_id from attributes, ImmuneSpanProcessor on_end/on_start |

## Gaps for Real-World Scenarios (and coverage added)

### 1. **ApiStore (server API mode)** — ✅ Covered

- **DOCS §5, §6:** Production uses `SERVER_API_BASE_URL` → ApiStore.
- **Coverage:** `tests/test_api_store.py` — headers (X-Run-Id, X-API-Key, Bearer), write_agent_vitals path/payload, get_recent params, HTTP error propagation.

### 2. **Web Dashboard & HTTP ingest** — ✅ Covered

- **DOCS §2.6:** Real agents report via `POST /api/v1/ingest`; dashboard approve/heal-now.
- **Coverage:** `tests/test_web_dashboard.py` — GET status, agents, stats, pending/rejected; POST ingest (valid, missing agent_id, invalid types); ingest requires X-API-KEY when configured; approve-healing and heal-explicitly (missing agent_id, no-pending/no-rejected).

### 3. **Store-backed detection path** — ✅ Covered

- **DOCS §3.3:** Sentinel reads from store via telemetry.get_recent().
- **Coverage:** `tests/test_store_backed.py` + `tests/store_helpers.py` — InMemoryStore; detection using vitals from store; orchestrator with store uses store for telemetry and detection.

### 4. **Run isolation (run_id)** — ✅ Covered

- **DOCS §4.5, §6:** Data scoped by run_id.
- **Coverage:** `tests/test_store_backed.py` — InMemoryStore keyed by run_id; vitals_isolated_by_run_id; orchestrator with store.

### 5. **Restart resilience (graceful degradation)** — ✅ Covered

- **DOCS §4.5:** No cache / cold start still learns baseline.
- **Coverage:** `tests/test_store_backed.py` — TestRestartResilience: orchestrator with store and no cache learns baseline after 20 samples.

### 6. **ImmuneMemory + Healer execution** — ✅ Covered

- **DOCS §4.3, §3.2:** Immune memory and store.write_healing_event.
- **Coverage:** `tests/test_memory.py` — record_healing/get_failed_actions in-memory; with store: write_healing_event called, get_failed_actions uses store. `tests/test_healing.py` — TestHealerExecution: apply_healing calls agent.state methods (reset_memory, revoke_tools, cure).

### 7. **Rejected → “Heal now”** — ✅ Covered

- **DOCS §4.2, §13.9:** Heal now removes from rejected and heals.
- **Coverage:** `tests/test_orchestrator.py` — test_rejected_then_heal_now_removes_from_rejected_and_heals.

### 8. **ChaosInjector**

- **DOCS §3.2:** Chaos injector in orchestrator. Optional; not yet covered.

### 9. **End-to-end with store**

- **DOCS §2.4, §3.3:** Full loop with persistent store and infection/healing events in store.
- **Gap:** Not yet a single E2E test with store asserting infection_event and healing_event written. Store-backed detection and orchestrator-with-store tests cover the main data path.

### 10. **LLM Gateway (passive observation)** — ✅ Covered

- **DOCS §8:** Gateway reverse proxy, fingerprinting, discovery, policy engine, management APIs.
- **Coverage:**
  - `tests/test_gateway_vitals.py` — System prompt extraction, tool-call counting, cost estimation, full vitals from request/response, streaming chunk extraction.
  - `tests/test_gateway_fingerprint.py` — X-Agent-ID header, API key hash, IP+UA fallback, priority order, agent type derivation.
  - `tests/test_gateway_discovery.py` — New agent creation, count increment, model/IP accumulation, type upgrade, callback on new, list/count.
  - `tests/test_gateway_policy.py` — Empty rules allow all, model block/allow lists, rate limiting, glob patterns, rule serialization.
  - `tests/test_gateway_app.py` — Health, agents, stats, policies, vitals, baseline management APIs; proxy passthrough; Cache-Control headers.
  - `tests/test_gateway_otel.py` — LLM span detection, span-to-vitals extraction, error status, agent_id from attributes, ImmuneSpanProcessor.

## Summary

- **Unit/component tests** for detection, baseline, cache, telemetry, diagnosis, healing policies, orchestrator, gateway, SDK, and the new production-readiness modules are in good shape for the scenarios described in DOCS.
- **Covered (items 1–10 above):** ApiStore contract, dashboard HTTP (ingest, approve, heal-now, read APIs), store-backed detection, run_id isolation, restart resilience (no cache), ImmuneMemory + Healer execution, rejected → Heal now flow, LLM Gateway (vitals extraction, fingerprinting, discovery, policy engine, management API, OTEL processor).
- **Production enforcement and lifecycle:** Multi-hypothesis diagnosis with ranked hypotheses and operator feedback, success-weighted action selection with cross-agent generalization, probation-based post-healing validation, 8-state lifecycle state machine, pluggable enforcement strategies (gateway, process, container, composite), pluggable healing executors (simulated, gateway, process, container), fleet-wide anomaly correlation, baseline adaptation (accelerate and hard-reset).
- **Optional / not yet covered:** ChaosInjector tests; single E2E test with persistent store asserting infection_event and healing_event written; MCP proxy integration tests (requires live MCP server).

**Test files:** `test_api_store.py`, `test_web_dashboard.py`, `test_store_backed.py`, `test_memory.py`, `store_helpers.py` (InMemoryStore), `test_gateway_vitals.py`, `test_gateway_fingerprint.py`, `test_gateway_discovery.py`, `test_gateway_policy.py`, `test_gateway_app.py`, `test_gateway_otel.py`, `test_lifecycle.py`, `test_enforcement.py`, `test_executor.py`, `test_correlator.py`, `test_diagnosis_multi.py`, `test_probation.py`, plus existing `test_*.py` for detection, baseline, cache, telemetry, diagnosis, healing, orchestrator, sdk.
