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
| **Healing policies** | `test_healing.py` | Policy ladder, next_action (skip failed), all diagnosis types have policies ending in RESET_AGENT |
| **Orchestrator (integration)** | `test_orchestrator.py` | Baseline learning, latency spike → infection, quarantine → cache persist/restore, **HITL**: severe → pending, approve, reject; **auto-heal**; deviation threshold |
| **SDK** | `test_sdk.py` | Payload construction, API key header, buffering, error callback; mocks HTTP to ingest |

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

## Summary

- **Unit/component tests** for detection, baseline, cache, telemetry, diagnosis, healing policies, and orchestrator (with in-memory store) are in good shape for the scenarios described in DOCS.
- **Covered (items 1–7 above):** ApiStore contract, dashboard HTTP (ingest, approve, heal-now, read APIs), store-backed detection, run_id isolation, restart resilience (no cache), ImmuneMemory + Healer execution, rejected → Heal now flow.
- **Optional / not yet covered:** ChaosInjector tests; single E2E test with persistent store asserting infection_event and healing_event written.

**Test files:** `test_api_store.py`, `test_web_dashboard.py`, `test_store_backed.py`, `test_memory.py`, `store_helpers.py` (InMemoryStore), plus existing `test_*.py` for detection, baseline, cache, telemetry, diagnosis, healing, orchestrator, sdk.
