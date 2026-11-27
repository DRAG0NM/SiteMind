"""Nexent live result sources and proposal routing for B17 scenario runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from plantkg.core.contracts import ErrorCode, PlantkgError
from plantkg.core.ports import AgentResultSource, ProposalSink
from plantkg.core.registry import Registry

SCENARIO_PLUGIN_HOOK = "scenario_plugin"


class RunSinkRouter:
    """Write live proposals to the current scenario run's sink."""

    def __init__(self, original: Any, scenario: Registry, current_run_id: Callable[[], str | None]) -> None:
        self._original, self._scenario, self._current_run_id = original, scenario, current_run_id

    def save(self, proposal: Any, binding: Any) -> str:
        target = self._scenario_sink(binding) or self._original
        return target.save(proposal, binding)

    def attach_reply(self, proposal_id: str, reply: Any) -> Any:
        return self._delegate("attach_reply", proposal_id, reply)

    def get(self, proposal_id: str) -> Any:
        return self._delegate("get", proposal_id)

    def list(self, ticket_id: str, round_: int | None = None) -> list[Any]:
        scenario = self._scenario.ports.get(ProposalSink)
        if scenario is not None and scenario is not self and scenario is not self._original:
            found = scenario.list(ticket_id, round_)
            if found:
                return found
        original = getattr(self._original, "list", None)
        return original(ticket_id, round_) if original is not None else []

    def _scenario_sink(self, binding: Any) -> Any:
        sink = self._scenario.ports.get(ProposalSink)
        if sink is None or sink is self or sink is self._original:
            return None
        run_id = getattr(binding, "run_id", None)
        return sink if run_id is not None and run_id == self._current_run_id() else None

    def _delegate(self, name: str, *args: Any) -> Any:
        sinks = [s for s in (self._scenario.ports.get(ProposalSink), self._original) if s is not None and s is not self]
        errors: list[PlantkgError] = []
        for sink in sinks:
            try:
                return getattr(sink, name)(*args)
            except PlantkgError as exc:
                errors.append(exc)
        if errors:
            raise errors[-1]
        raise PlantkgError(ErrorCode.NOT_FOUND, f"提案 {args[0] if args else ''} 不存在")


def register_live_sources(source: Registry) -> Callable[[Registry], None]:
    """Expose installed Nexent live modes through the scenario registry hook."""
    def plugin(scenario: Registry) -> None:
        for (port, mode, key), impl in source.mode_ports.items():
            if port is AgentResultSource and mode == "live":
                scenario.register_mode(port, mode, impl, key=key)
    return plugin
