"""
Fleet Correlator — Detect fleet-wide anomaly patterns before diagnosing
individual agents.

If many agents exhibit the same anomaly simultaneously, the root cause is
almost certainly external (LLM provider outage, network issue, infrastructure
problem) rather than agent-specific.  Quarantining individual agents in that
scenario wastes healing effort and creates unnecessary downtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from .detection import AnomalyType, InfectionReport, Sentinel
from .logging_config import get_logger

logger = get_logger("correlator")


class CorrelationVerdict(Enum):
    AGENT_SPECIFIC = "agent_specific"
    FLEET_WIDE = "fleet_wide"
    PARTIAL_FLEET = "partial_fleet"


@dataclass
class CorrelationResult:
    verdict: CorrelationVerdict
    affected_fraction: float
    common_anomalies: List[AnomalyType]
    affected_agents: List[str]
    detail: str = ""


_FLEET_WIDE_THRESHOLD = 0.30
_PARTIAL_FLEET_THRESHOLD = 0.15


class FleetCorrelator:
    """Checks whether an anomaly is isolated or fleet-wide.

    Parameters
    ----------
    fleet_wide_threshold : float
        Fraction of monitored agents that must show overlapping anomaly types
        for the incident to be classified as ``FLEET_WIDE`` (default 30 %).
    partial_threshold : float
        Fraction for ``PARTIAL_FLEET`` classification (default 15 %).
    """

    def __init__(
        self,
        fleet_wide_threshold: float = _FLEET_WIDE_THRESHOLD,
        partial_threshold: float = _PARTIAL_FLEET_THRESHOLD,
    ):
        self.fleet_wide_threshold = fleet_wide_threshold
        self.partial_threshold = partial_threshold

    def correlate(
        self,
        infection: InfectionReport,
        all_agents: Dict[str, Any],
        sentinel: Sentinel,
        baselines: Dict[str, Any],
        telemetry,
    ) -> CorrelationResult:
        """Compare *infection* against the current state of the whole fleet.

        Parameters
        ----------
        infection : InfectionReport
            The anomaly detected for one particular agent.
        all_agents : dict
            ``{agent_id: agent_obj}`` for every registered agent.
        sentinel : Sentinel
            The anomaly detector instance.
        baselines : dict
            ``{agent_id: BaselineProfile}`` from the baseline learner.
        telemetry
            TelemetryCollector, used to fetch recent vitals per agent.
        """
        target_anomalies: Set[AnomalyType] = set(infection.anomalies)
        target_id = infection.agent_id

        monitored_count = 0
        affected: List[str] = []
        common: Set[AnomalyType] = set()

        for aid in all_agents:
            if aid == target_id:
                continue
            bl = baselines.get(aid)
            if not bl:
                continue
            monitored_count += 1

            recent = telemetry.get_recent(aid, window_seconds=10)
            if not recent:
                continue

            other = sentinel.detect_infection(recent, bl)
            if other is None:
                continue

            overlap = target_anomalies & set(other.anomalies)
            if overlap:
                affected.append(aid)
                common.update(overlap)

        if monitored_count == 0:
            return CorrelationResult(
                verdict=CorrelationVerdict.AGENT_SPECIFIC,
                affected_fraction=0.0,
                common_anomalies=list(target_anomalies),
                affected_agents=[],
                detail="no other monitored agents",
            )

        fraction = len(affected) / monitored_count

        if fraction >= self.fleet_wide_threshold:
            verdict = CorrelationVerdict.FLEET_WIDE
            detail = (
                f"{len(affected)}/{monitored_count} agents ({fraction:.0%}) show "
                f"overlapping anomalies — likely external cause"
            )
            logger.warning("Fleet-wide anomaly: %s", detail)
        elif fraction >= self.partial_threshold:
            verdict = CorrelationVerdict.PARTIAL_FLEET
            detail = (
                f"{len(affected)}/{monitored_count} agents ({fraction:.0%}) affected — "
                f"possible partial outage"
            )
            logger.info("Partial fleet anomaly: %s", detail)
        else:
            verdict = CorrelationVerdict.AGENT_SPECIFIC
            detail = f"only {len(affected)}/{monitored_count} other agents affected"

        return CorrelationResult(
            verdict=verdict,
            affected_fraction=fraction,
            common_anomalies=sorted(common, key=lambda a: a.value),
            affected_agents=affected,
            detail=detail,
        )
