"""Small standard-library agent runtime with an auditable event stream."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from ..data import SHANGHAI


@dataclass(frozen=True)
class AgentEvent:
    agent: str
    role: str
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    summary: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    def as_dict(self):
        return {"agent": self.agent, "role": self.role, "status": self.status,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "duration_ms": self.duration_ms, "summary": self.summary,
                "inputs": list(self.inputs), "outputs": list(self.outputs)}


@dataclass
class AgentContext:
    mode: str
    config: dict
    run_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=lambda: datetime.now(SHANGHAI).isoformat())
    frame: dict | None = None
    report: dict | None = None
    assessments: dict | None = None
    connection: dict | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    events: list[AgentEvent] = field(default_factory=list)

    def public_trace(self):
        """Safe trace for reports: no clients, credentials or raw exceptions."""
        return {"schema_version": 1, "run_id": self.run_id, "mode": self.mode,
                "started_at": self.started_at, "status": "completed",
                "events": [event.as_dict() for event in self.events],
                "artifacts": dict(self.artifacts)}


class Agent(Protocol):
    name: str
    role: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    def run(self, context: AgentContext) -> str: ...


class AgentPipeline:
    """Run deterministic agents in order and record bounded safe metadata."""
    def __init__(self, agents):
        self.agents = tuple(agents)
        names = [agent.name for agent in self.agents]
        if not names or len(names) != len(set(names)):
            raise ValueError("Agent pipeline requires unique, non-empty agents")

    def run(self, context: AgentContext):
        for agent in self.agents:
            started = datetime.now(SHANGHAI)
            clock = monotonic()
            try:
                summary = agent.run(context)
            except Exception:
                finished = datetime.now(SHANGHAI)
                context.events.append(AgentEvent(
                    agent=agent.name, role=agent.role, status="failed",
                    started_at=started.isoformat(), finished_at=finished.isoformat(),
                    duration_ms=max(0, round((monotonic() - clock) * 1000)),
                    summary="Agent 执行失败；详细错误仅由命令行边界处理。",
                    inputs=agent.inputs, outputs=()))
                raise
            finished = datetime.now(SHANGHAI)
            context.events.append(AgentEvent(
                agent=agent.name, role=agent.role, status="completed",
                started_at=started.isoformat(), finished_at=finished.isoformat(),
                duration_ms=max(0, round((monotonic() - clock) * 1000)),
                summary=str(summary)[:240], inputs=agent.inputs, outputs=agent.outputs))
        for agent in self.agents:
            finalize = getattr(agent, "finalize", None)
            if callable(finalize):
                finalize(context)
        return context

    def describe(self):
        return [{"name": agent.name, "role": agent.role,
                 "inputs": list(agent.inputs), "outputs": list(agent.outputs)}
                for agent in self.agents]
