"""
Enforcement Strategies — Pluggable mechanisms that actually block / unblock
agent execution in the real world.

Four concrete strategies are provided:

* **GatewayEnforcement** — injects blocking policy rules into the LLM Gateway.
* **ProcessEnforcement** — sends OS signals (SIGSTOP / SIGCONT / SIGTERM).
* **ContainerEnforcement** — pauses/unpauses Docker containers or scales K8s.
* **CompositeEnforcement** — chains multiple strategies in priority order.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .logging_config import get_logger

logger = get_logger("enforcement")


@dataclass
class EnforcementResult:
    success: bool
    strategy: str
    agent_id: str
    action: str
    detail: str = ""


class EnforcementStrategy(ABC):
    """Base class for all enforcement strategies."""

    name: str = "base"

    @abstractmethod
    async def block(self, agent_id: str, reason: str) -> EnforcementResult:
        """Fully block an agent from executing."""

    @abstractmethod
    async def unblock(self, agent_id: str) -> EnforcementResult:
        """Remove the block so the agent can execute again."""

    @abstractmethod
    async def drain(self, agent_id: str, timeout_s: float) -> EnforcementResult:
        """Gracefully drain: block new work, let in-flight finish, then block."""

    @abstractmethod
    async def health_check(self, agent_id: str) -> Dict[str, Any]:
        """Return health / reachability info for the agent."""


# ── Gateway enforcement ──────────────────────────────────────────────


class GatewayEnforcement(EnforcementStrategy):
    """Block agent traffic at the LLM Gateway by injecting policy rules.

    Requires a reference to the gateway's ``PolicyEngine`` instance so it can
    add/remove blocking rules dynamically.
    """

    name = "gateway"

    def __init__(self, policy_engine=None):
        self._policy = policy_engine
        self._blocked: Dict[str, str] = {}

    def set_policy_engine(self, engine):
        self._policy = engine

    async def block(self, agent_id: str, reason: str) -> EnforcementResult:
        if self._policy is None:
            return EnforcementResult(False, self.name, agent_id, "block", "no policy engine")
        from gateway.policy import PolicyRule
        rule_name = f"quarantine:{agent_id}"
        rule = PolicyRule(
            name=rule_name,
            agent_pattern=agent_id,
            action_on_violation="block",
        )
        self._policy.add_rule(rule)
        self._blocked[agent_id] = rule_name
        logger.info("Gateway BLOCK: agent=%s reason=%s", agent_id, reason)
        return EnforcementResult(True, self.name, agent_id, "block", rule_name)

    async def unblock(self, agent_id: str) -> EnforcementResult:
        if self._policy is None:
            return EnforcementResult(False, self.name, agent_id, "unblock", "no policy engine")
        rule_name = self._blocked.pop(agent_id, f"quarantine:{agent_id}")
        self._policy.remove_rule(rule_name)
        logger.info("Gateway UNBLOCK: agent=%s", agent_id)
        return EnforcementResult(True, self.name, agent_id, "unblock", rule_name)

    async def drain(self, agent_id: str, timeout_s: float) -> EnforcementResult:
        await self.block(agent_id, reason="draining")
        await asyncio.sleep(min(timeout_s, 5.0))
        return EnforcementResult(True, self.name, agent_id, "drain", f"timeout={timeout_s}s")

    async def health_check(self, agent_id: str) -> Dict[str, Any]:
        blocked = agent_id in self._blocked
        return {"strategy": self.name, "agent_id": agent_id, "blocked": blocked}


# ── Process enforcement ──────────────────────────────────────────────


class ProcessEnforcement(EnforcementStrategy):
    """OS-level process control via signals.

    Agents must register their PID with :meth:`register_pid` so the immune
    system knows which process to signal.
    """

    name = "process"

    def __init__(self):
        self._pids: Dict[str, int] = {}

    def register_pid(self, agent_id: str, pid: int):
        self._pids[agent_id] = pid
        logger.info("Process registered: agent=%s pid=%d", agent_id, pid)

    def unregister_pid(self, agent_id: str):
        self._pids.pop(agent_id, None)

    def _get_pid(self, agent_id: str) -> Optional[int]:
        return self._pids.get(agent_id)

    async def block(self, agent_id: str, reason: str) -> EnforcementResult:
        pid = self._get_pid(agent_id)
        if pid is None:
            return EnforcementResult(False, self.name, agent_id, "block", "pid_not_registered")
        try:
            os.kill(pid, signal.SIGSTOP)
            logger.info("Process SIGSTOP: agent=%s pid=%d reason=%s", agent_id, pid, reason)
            return EnforcementResult(True, self.name, agent_id, "block", f"SIGSTOP pid={pid}")
        except OSError as exc:
            logger.error("SIGSTOP failed for pid=%d: %s", pid, exc)
            return EnforcementResult(False, self.name, agent_id, "block", str(exc))

    async def unblock(self, agent_id: str) -> EnforcementResult:
        pid = self._get_pid(agent_id)
        if pid is None:
            return EnforcementResult(False, self.name, agent_id, "unblock", "pid_not_registered")
        try:
            os.kill(pid, signal.SIGCONT)
            logger.info("Process SIGCONT: agent=%s pid=%d", agent_id, pid)
            return EnforcementResult(True, self.name, agent_id, "unblock", f"SIGCONT pid={pid}")
        except OSError as exc:
            logger.error("SIGCONT failed for pid=%d: %s", pid, exc)
            return EnforcementResult(False, self.name, agent_id, "unblock", str(exc))

    async def drain(self, agent_id: str, timeout_s: float) -> EnforcementResult:
        pid = self._get_pid(agent_id)
        if pid is None:
            return EnforcementResult(False, self.name, agent_id, "drain", "pid_not_registered")
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pass
        await asyncio.sleep(min(timeout_s, 30.0))
        return await self.block(agent_id, reason="drain_timeout")

    async def health_check(self, agent_id: str) -> Dict[str, Any]:
        pid = self._get_pid(agent_id)
        if pid is None:
            return {"strategy": self.name, "agent_id": agent_id, "registered": False}
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
        return {"strategy": self.name, "agent_id": agent_id, "pid": pid, "alive": alive}


# ── Container enforcement ────────────────────────────────────────────


class ContainerEnforcement(EnforcementStrategy):
    """Container-level control via Docker CLI or ``kubectl``.

    Agents register either a Docker container ID or a Kubernetes
    deployment/namespace pair.
    """

    name = "container"

    def __init__(self):
        self._containers: Dict[str, Dict[str, str]] = {}

    def register_container(self, agent_id: str, container_id: str):
        self._containers[agent_id] = {"type": "docker", "id": container_id}

    def register_k8s(self, agent_id: str, namespace: str, deployment: str):
        self._containers[agent_id] = {
            "type": "k8s",
            "namespace": namespace,
            "deployment": deployment,
        }

    async def block(self, agent_id: str, reason: str) -> EnforcementResult:
        info = self._containers.get(agent_id)
        if not info:
            return EnforcementResult(False, self.name, agent_id, "block", "not_registered")

        if info["type"] == "docker":
            return await self._docker_cmd("pause", info["id"], agent_id, "block")
        return await self._k8s_scale(info, 0, agent_id, "block")

    async def unblock(self, agent_id: str) -> EnforcementResult:
        info = self._containers.get(agent_id)
        if not info:
            return EnforcementResult(False, self.name, agent_id, "unblock", "not_registered")

        if info["type"] == "docker":
            return await self._docker_cmd("unpause", info["id"], agent_id, "unblock")
        return await self._k8s_scale(info, 1, agent_id, "unblock")

    async def drain(self, agent_id: str, timeout_s: float) -> EnforcementResult:
        await asyncio.sleep(min(timeout_s, 30.0))
        return await self.block(agent_id, reason="drain_timeout")

    async def health_check(self, agent_id: str) -> Dict[str, Any]:
        info = self._containers.get(agent_id)
        return {"strategy": self.name, "agent_id": agent_id, "registered": info is not None, "info": info}

    @staticmethod
    async def _docker_cmd(cmd: str, container_id: str, agent_id: str, action: str) -> EnforcementResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", cmd, container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            ok = proc.returncode == 0
            detail = f"docker {cmd} {container_id}"
            if not ok:
                detail += f" stderr={stderr.decode().strip()}"
            logger.info("Container %s: agent=%s ok=%s", cmd, agent_id, ok)
            return EnforcementResult(ok, "container", agent_id, action, detail)
        except FileNotFoundError:
            return EnforcementResult(False, "container", agent_id, action, "docker_not_found")

    @staticmethod
    async def _k8s_scale(info: dict, replicas: int, agent_id: str, action: str) -> EnforcementResult:
        ns = info["namespace"]
        dep = info["deployment"]
        try:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "scale", f"--replicas={replicas}",
                f"deployment/{dep}", "-n", ns,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            ok = proc.returncode == 0
            detail = f"kubectl scale --replicas={replicas} deployment/{dep} -n {ns}"
            if not ok:
                detail += f" stderr={stderr.decode().strip()}"
            logger.info("K8s scale: agent=%s replicas=%d ok=%s", agent_id, replicas, ok)
            return EnforcementResult(ok, "container", agent_id, action, detail)
        except FileNotFoundError:
            return EnforcementResult(False, "container", agent_id, action, "kubectl_not_found")


# ── Composite enforcement ────────────────────────────────────────────


class CompositeEnforcement(EnforcementStrategy):
    """Chains multiple strategies.  Tries each in order; stops at first success."""

    name = "composite"

    def __init__(self, strategies: Optional[List[EnforcementStrategy]] = None):
        self.strategies: List[EnforcementStrategy] = strategies or []

    def add(self, strategy: EnforcementStrategy):
        self.strategies.append(strategy)

    async def block(self, agent_id: str, reason: str) -> EnforcementResult:
        for s in self.strategies:
            result = await s.block(agent_id, reason)
            if result.success:
                return result
        return EnforcementResult(False, self.name, agent_id, "block", "all_strategies_failed")

    async def unblock(self, agent_id: str) -> EnforcementResult:
        for s in self.strategies:
            result = await s.unblock(agent_id)
            if result.success:
                return result
        return EnforcementResult(False, self.name, agent_id, "unblock", "all_strategies_failed")

    async def drain(self, agent_id: str, timeout_s: float) -> EnforcementResult:
        for s in self.strategies:
            result = await s.drain(agent_id, timeout_s)
            if result.success:
                return result
        return EnforcementResult(False, self.name, agent_id, "drain", "all_strategies_failed")

    async def health_check(self, agent_id: str) -> Dict[str, Any]:
        checks = {}
        for s in self.strategies:
            checks[s.name] = await s.health_check(agent_id)
        return {"strategy": self.name, "agent_id": agent_id, "sub_checks": checks}


class NoOpEnforcement(EnforcementStrategy):
    """In-memory only enforcement for simulations and testing."""

    name = "noop"
    _blocked: Dict[str, bool] = {}

    async def block(self, agent_id: str, reason: str) -> EnforcementResult:
        self._blocked[agent_id] = True
        return EnforcementResult(True, self.name, agent_id, "block", "simulated")

    async def unblock(self, agent_id: str) -> EnforcementResult:
        self._blocked.pop(agent_id, None)
        return EnforcementResult(True, self.name, agent_id, "unblock", "simulated")

    async def drain(self, agent_id: str, timeout_s: float) -> EnforcementResult:
        self._blocked[agent_id] = True
        return EnforcementResult(True, self.name, agent_id, "drain", "simulated")

    async def health_check(self, agent_id: str) -> Dict[str, Any]:
        return {"strategy": self.name, "agent_id": agent_id, "blocked": self._blocked.get(agent_id, False)}
