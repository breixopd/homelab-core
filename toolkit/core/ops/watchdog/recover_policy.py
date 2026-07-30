"""Pure, node-local policy for non-destructive watchdog recovery."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecoverAutoConfig:
    """Guardrails for watchdog-triggered machine recovery."""

    enabled: bool = True
    cooldown_seconds: int = 3600
    terminal_threshold: int = 3
    multi_failure_min: int = 3
    restart_budget: int = 3


@dataclass(frozen=True, slots=True)
class RecoverSignal:
    """Current health state needed by the recovery verdict."""

    service: str
    severity: str
    terminal: bool = False
    restart_count: int = 0


@dataclass(frozen=True, slots=True)
class RecoverDecision:
    trigger: bool
    reason: str
    destroy_first: bool = False


def _node_decision(
    signals: list[RecoverSignal],
    *,
    last_recover_at: float | None,
    now: float,
    cfg: RecoverAutoConfig,
) -> RecoverDecision:
    if not cfg.enabled:
        return RecoverDecision(False, "automatic recovery disabled")

    if last_recover_at is not None and (now - last_recover_at) < cfg.cooldown_seconds:
        remaining = max(0, int(cfg.cooldown_seconds - (now - last_recover_at)))
        return RecoverDecision(False, f"automatic recovery cooldown ({remaining}s remaining)")

    critical = {signal.service for signal in signals if signal.severity == "critical"}
    terminal = {signal.service for signal in signals if signal.terminal or signal.restart_count >= cfg.restart_budget}
    if len(terminal) >= cfg.terminal_threshold:
        return RecoverDecision(True, f"{len(terminal)} terminal service failures")
    if len(critical) >= cfg.multi_failure_min:
        return RecoverDecision(True, f"{len(critical)} critical service failures")
    return RecoverDecision(False, "node failure thresholds not met")


def recover_decisions(
    signals: Iterable[RecoverSignal],
    *,
    vm_for_service: dict[str, str],
    now: float,
    last_recover_at: dict[str, float] | None = None,
    cfg: RecoverAutoConfig | None = None,
) -> dict[str, RecoverDecision]:
    """Return an independent recovery verdict for every affected node.

    Only current report signals participate. Services without a known node are
    deliberately excluded because an unattended broad remedy must have an
    unambiguous target.
    """

    grouped: dict[str, list[RecoverSignal]] = defaultdict(list)
    for signal in signals:
        vm = vm_for_service.get(signal.service)
        if vm:
            grouped[vm].append(signal)

    policy = cfg or RecoverAutoConfig()
    cooldowns = last_recover_at or {}
    return {
        vm: _node_decision(
            node_signals,
            last_recover_at=cooldowns.get(vm),
            now=now,
            cfg=policy,
        )
        for vm, node_signals in sorted(grouped.items())
    }


__all__ = [
    "RecoverAutoConfig",
    "RecoverDecision",
    "RecoverSignal",
    "recover_decisions",
]
