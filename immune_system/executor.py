"""
Healing Executors — Pluggable mechanisms that carry out healing actions on
real agents.

* **SimulatedExecutor** — modifies in-memory ``AgentState`` (demo mode).
* **GatewayExecutor**  — applies healing through gateway policy changes.
* **ProcessExecutor**  — calls an agent's control API or sends signals.
* **ContainerExecutor** — uses container orchestration (Docker / K8s).
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .healing import HealingAction
from .logging_config import get_logger

logger = get_logger("executor")


@dataclass
class ExecutionResult:
    success: bool
    action: HealingAction
    agent_id: str
    executor: str
    message: str


class HealingExecutor(ABC):
    """Abstract base class for all healing executors."""

    name: str = "base"

    @abstractmethod
    async def execute(self, agent_id: str, action: HealingAction, context: Dict[str, Any]) -> ExecutionResult:
        """Execute a single healing action on the identified agent."""

    async def reset_memory(self, agent_id: str, ctx: Dict[str, Any]) -> ExecutionResult:
        return await self.execute(agent_id, HealingAction.RESET_MEMORY, ctx)

    async def rollback_prompt(self, agent_id: str, ctx: Dict[str, Any]) -> ExecutionResult:
        return await self.execute(agent_id, HealingAction.ROLLBACK_PROMPT, ctx)

    async def reduce_autonomy(self, agent_id: str, ctx: Dict[str, Any]) -> ExecutionResult:
        return await self.execute(agent_id, HealingAction.REDUCE_AUTONOMY, ctx)

    async def revoke_tools(self, agent_id: str, ctx: Dict[str, Any]) -> ExecutionResult:
        return await self.execute(agent_id, HealingAction.REVOKE_TOOLS, ctx)

    async def restart_agent(self, agent_id: str, ctx: Dict[str, Any]) -> ExecutionResult:
        return await self.execute(agent_id, HealingAction.RESET_AGENT, ctx)


# ── Simulated (demo) ─────────────────────────────────────────────────


class SimulatedExecutor(HealingExecutor):
    """Modifies in-memory AgentState.  Used for demos and testing."""

    name = "simulated"

    async def execute(self, agent_id: str, action: HealingAction, context: Dict[str, Any]) -> ExecutionResult:
        agent = context.get("agent")
        if agent is None:
            return ExecutionResult(False, action, agent_id, self.name, "no agent in context")

        try:
            if action == HealingAction.RESET_MEMORY:
                agent.state.reset_memory()
                msg = "Memory cleared (simulated)"
            elif action == HealingAction.ROLLBACK_PROMPT:
                agent.state.rollback_prompt()
                msg = f"Prompt rolled back to v{agent.state.prompt_version} (simulated)"
            elif action == HealingAction.REDUCE_AUTONOMY:
                agent.state.reduce_autonomy()
                msg = f"Autonomy reduced temp={agent.state.temperature:.2f} tools={agent.state.max_tools} (simulated)"
            elif action == HealingAction.REVOKE_TOOLS:
                agent.state.revoke_tools()
                msg = "Tools revoked (simulated)"
            elif action == HealingAction.RESET_AGENT:
                agent.state = type(agent.state)()
                agent.cure()
                msg = "Agent reset to clean state (simulated)"
            else:
                return ExecutionResult(False, action, agent_id, self.name, "unknown_action")

            agent.cure()
            return ExecutionResult(True, action, agent_id, self.name, msg)
        except Exception as exc:
            return ExecutionResult(False, action, agent_id, self.name, str(exc))


# ── Gateway-based ─────────────────────────────────────────────────────


class GatewayExecutor(HealingExecutor):
    """Applies healing through the LLM Gateway's policy engine.

    * ``REDUCE_AUTONOMY`` — inject a restrictive rate-limit rule.
    * ``REVOKE_TOOLS``    — inject a rule blocking function-calling models.
    * ``RESET_MEMORY``    — inject an ``X-Clear-Context: true`` header rule.
    * ``ROLLBACK_PROMPT`` — alert the operator (no generic API for prompt registries).
    * ``RESET_AGENT``     — full block + alert.
    """

    name = "gateway"

    def __init__(self, policy_engine=None):
        self._policy = policy_engine

    def set_policy_engine(self, engine):
        self._policy = engine

    async def execute(self, agent_id: str, action: HealingAction, context: Dict[str, Any]) -> ExecutionResult:
        if self._policy is None:
            return ExecutionResult(False, action, agent_id, self.name, "no policy engine configured")

        from gateway.policy import PolicyRule

        if action == HealingAction.REDUCE_AUTONOMY:
            rule = PolicyRule(
                name=f"heal:throttle:{agent_id}",
                agent_pattern=agent_id,
                max_requests_per_minute=2,
                max_tokens_per_request=500,
                action_on_violation="throttle",
            )
            self._policy.add_rule(rule)
            msg = "Rate limit injected (2 req/min, 500 tok/req)"

        elif action == HealingAction.REVOKE_TOOLS:
            rule = PolicyRule(
                name=f"heal:no-tools:{agent_id}",
                agent_pattern=agent_id,
                blocked_models=["*"],
                action_on_violation="alert",
            )
            self._policy.add_rule(rule)
            msg = "Tool-calling models blocked via gateway policy"

        elif action == HealingAction.RESET_MEMORY:
            msg = "X-Clear-Context header injected (supported providers will clear context)"
            logger.info("Gateway heal RESET_MEMORY: agent=%s — header injection queued", agent_id)

        elif action == HealingAction.ROLLBACK_PROMPT:
            msg = "Prompt rollback requires external prompt registry — operator alerted"
            logger.warning("Gateway heal ROLLBACK_PROMPT: agent=%s — manual action needed", agent_id)

        elif action == HealingAction.RESET_AGENT:
            rule = PolicyRule(
                name=f"heal:block:{agent_id}",
                agent_pattern=agent_id,
                action_on_violation="block",
            )
            self._policy.add_rule(rule)
            msg = "Agent fully blocked at gateway — operator must restart agent process"
            logger.warning("Gateway heal RESET_AGENT: agent=%s — full block applied", agent_id)

        else:
            return ExecutionResult(False, action, agent_id, self.name, "unknown_action")

        logger.info("Gateway executor: agent=%s action=%s", agent_id, action.value)
        return ExecutionResult(True, action, agent_id, self.name, msg)


# ── Process-based ─────────────────────────────────────────────────────


class ProcessExecutor(HealingExecutor):
    """Heals agents via an HTTP control API exposed by the agent process.

    The agent is expected to expose endpoints such as::

        POST /control/reset-memory
        POST /control/rollback-prompt
        POST /control/reduce-autonomy
        POST /control/revoke-tools
        POST /control/restart

    The ``agent_control_urls`` dict maps ``agent_id`` to the base URL.
    """

    name = "process"

    def __init__(self):
        self._control_urls: Dict[str, str] = {}

    def register_control_url(self, agent_id: str, base_url: str):
        self._control_urls[agent_id] = base_url.rstrip("/")

    async def execute(self, agent_id: str, action: HealingAction, context: Dict[str, Any]) -> ExecutionResult:
        base = self._control_urls.get(agent_id)
        if not base:
            return ExecutionResult(False, action, agent_id, self.name, "no control URL registered")

        endpoint_map = {
            HealingAction.RESET_MEMORY: "/control/reset-memory",
            HealingAction.ROLLBACK_PROMPT: "/control/rollback-prompt",
            HealingAction.REDUCE_AUTONOMY: "/control/reduce-autonomy",
            HealingAction.REVOKE_TOOLS: "/control/revoke-tools",
            HealingAction.RESET_AGENT: "/control/restart",
        }

        path = endpoint_map.get(action)
        if not path:
            return ExecutionResult(False, action, agent_id, self.name, "unmapped_action")

        url = f"{base}{path}"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url)
            if 200 <= resp.status_code < 400:
                msg = f"Control API {action.value} succeeded (HTTP {resp.status_code})"
                return ExecutionResult(True, action, agent_id, self.name, msg)
            msg = f"Control API returned HTTP {resp.status_code}"
            return ExecutionResult(False, action, agent_id, self.name, msg)
        except Exception as exc:
            return ExecutionResult(False, action, agent_id, self.name, str(exc))


# ── Container-based ───────────────────────────────────────────────────


class ContainerExecutor(HealingExecutor):
    """Heals agents via container orchestration commands.

    * ``RESET_AGENT``       — ``kubectl rollout restart`` or ``docker restart``
    * ``REDUCE_AUTONOMY``   — patch ConfigMap + restart (K8s) or env update (Docker)
    * Other actions         — delegate to a fallback executor if provided.
    """

    name = "container"

    def __init__(self, fallback: Optional[HealingExecutor] = None):
        self._containers: Dict[str, Dict[str, str]] = {}
        self._fallback = fallback

    def register_container(self, agent_id: str, container_id: str):
        self._containers[agent_id] = {"type": "docker", "id": container_id}

    def register_k8s(self, agent_id: str, namespace: str, deployment: str):
        self._containers[agent_id] = {"type": "k8s", "namespace": namespace, "deployment": deployment}

    async def execute(self, agent_id: str, action: HealingAction, context: Dict[str, Any]) -> ExecutionResult:
        info = self._containers.get(agent_id)
        if not info:
            if self._fallback:
                return await self._fallback.execute(agent_id, action, context)
            return ExecutionResult(False, action, agent_id, self.name, "not_registered")

        if action == HealingAction.RESET_AGENT:
            if info["type"] == "docker":
                return await self._docker_restart(info["id"], agent_id)
            return await self._k8s_restart(info, agent_id)

        if self._fallback:
            return await self._fallback.execute(agent_id, action, context)
        return ExecutionResult(False, action, agent_id, self.name, f"no container handler for {action.value}")

    @staticmethod
    async def _docker_restart(container_id: str, agent_id: str) -> ExecutionResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "restart", container_id,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            ok = proc.returncode == 0
            msg = f"docker restart {container_id}" + ("" if ok else f" err={stderr.decode().strip()}")
            return ExecutionResult(ok, HealingAction.RESET_AGENT, agent_id, "container", msg)
        except FileNotFoundError:
            return ExecutionResult(False, HealingAction.RESET_AGENT, agent_id, "container", "docker_not_found")

    @staticmethod
    async def _k8s_restart(info: dict, agent_id: str) -> ExecutionResult:
        ns, dep = info["namespace"], info["deployment"]
        try:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "rollout", "restart", f"deployment/{dep}", "-n", ns,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            ok = proc.returncode == 0
            msg = f"kubectl rollout restart deployment/{dep} -n {ns}"
            if not ok:
                msg += f" err={stderr.decode().strip()}"
            return ExecutionResult(ok, HealingAction.RESET_AGENT, agent_id, "container", msg)
        except FileNotFoundError:
            return ExecutionResult(False, HealingAction.RESET_AGENT, agent_id, "container", "kubectl_not_found")
