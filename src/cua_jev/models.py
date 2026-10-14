from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Channel(StrEnum):
    GUI = "gui"
    CLI = "cli"
    MCP = "mcp"
    SCRIPT = "script"
    API = "api"
    CONTROL = "control"


class Risk(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    DESTRUCTIVE = "destructive"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


def _plain(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class Observation:
    task: str
    subgoal: str
    state: dict[str, Any]
    source: str = "predefined"
    observation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


@dataclass(frozen=True)
class ActionCandidate:
    id: str
    channel: Channel
    capability: str
    description: str
    arguments: dict[str, Any] = field(default_factory=dict)
    risk: Risk = Risk.READ_ONLY
    preconditions: tuple[str, ...] = ()
    verifier: str | None = None
    expected: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    intent: str = "act"

    def __post_init__(self) -> None:
        if not self.id or not self.capability or not self.description or not self.intent:
            raise ValueError("candidate id, capability, description and intent are required")
        json.dumps(self.arguments)

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


@dataclass(frozen=True)
class Decision:
    observation_id: str
    candidate_id: str
    probabilities: dict[str, float]
    confidence: float
    model: str
    latency_ms: float
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        values = [self.confidence, *self.probabilities.values()]
        if not values or not all(
            type(v) in (float, int) and math.isfinite(v) and 0 <= v <= 1 for v in values
        ):
            raise ValueError("invalid decision probabilities")
        if abs(sum(self.probabilities.values()) - 1) > 0.02:
            raise ValueError("probabilities do not sum to one")
        if self.candidate_id not in self.probabilities:
            raise ValueError("selected candidate missing from probabilities")
        if self.probabilities[self.candidate_id] < max(self.probabilities.values()) - 1e-6:
            raise ValueError("selected candidate is not the maximum-probability choice")

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


@dataclass(frozen=True)
class JevDecision:
    """Auditable binding of an API choice to the exact scene and offered actions."""

    decision: Decision
    scene_fingerprint: str
    candidate_set_hash: str
    offered_candidate_ids: tuple[str, ...]
    plan_contract_id: str | None = None
    request_hash: str | None = None

    @classmethod
    def bind(cls, decision: Decision, observation: Observation,
             candidates: list[ActionCandidate] | tuple[ActionCandidate, ...],
             request_hash: str | None = None) -> JevDecision:
        payload = [candidate.to_dict() for candidate in candidates]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        contract_ids = {candidate.arguments.get("plan_contract_id") for candidate in candidates}
        contract_id = next(iter(contract_ids)) if len(contract_ids) == 1 else None
        return cls(decision, state_fingerprint(observation), digest,
                   tuple(candidate.id for candidate in candidates), contract_id, request_hash)

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


@dataclass(frozen=True)
class ActionReceipt:
    observation_id: str
    decision_id: str
    candidate_id: str
    channel: Channel
    capability: str
    success: bool
    started_at: float
    ended_at: float
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    receipt_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000

    def to_dict(self) -> dict[str, Any]:
        data = _plain(asdict(self))
        data["duration_ms"] = self.duration_ms
        return data


@dataclass(frozen=True)
class Verification:
    passed: bool
    verifier: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))


def state_fingerprint(observation: Observation) -> str:
    # Volatile ids and timestamps must not hide an unchanged environment state.
    state = {key: value for key, value in observation.state.items() if key != "recent_actions"}
    stable = {
        "task": observation.task,
        "subgoal": observation.subgoal,
        "source": observation.source,
        "state": state,
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()
