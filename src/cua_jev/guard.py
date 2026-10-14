from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .errors import GuardRejected, ObservationExpired
from .models import ActionCandidate, Channel, Decision, Observation, Risk


class ActionGuard:
    """Fail-closed validation before any executor can mutate state."""

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str | Path] = (),
        allow_writes: bool = False,
        allow_destructive: bool = False,
        confirmed_candidates: Sequence[str] = (),
        max_observation_age_s: float = 30,
        precondition_evaluator: Callable[[str, Observation, ActionCandidate], bool] | None = None,
    ) -> None:
        if max_observation_age_s <= 0:
            raise ValueError("observation maximum age must be positive")
        self.max_observation_age_s = max_observation_age_s
        self.allowed_roots = tuple(Path(path).resolve() for path in allowed_roots)
        self.allow_writes = allow_writes
        self.allow_destructive = allow_destructive
        self.confirmed_candidates = set(confirmed_candidates)
        self.precondition_evaluator = precondition_evaluator
        self._consumed_observations: set[str] = set()

    def approve(
        self,
        observation: Observation,
        decision: Decision,
        candidates: Sequence[ActionCandidate],
    ) -> ActionCandidate:
        if decision.observation_id != observation.observation_id:
            raise GuardRejected("stale decision")
        if observation.observation_id in self._consumed_observations:
            raise GuardRejected("observation already consumed")
        matches = [candidate for candidate in candidates if candidate.id == decision.candidate_id]
        if len(matches) != 1:
            raise GuardRejected("decision does not identify one offered candidate")
        candidate = matches[0]
        self.check_fresh(observation, (candidate,))
        if candidate.requires_confirmation and candidate.id not in self.confirmed_candidates:
            raise GuardRejected("candidate requires explicit confirmation")
        if candidate.risk == Risk.LOCAL_WRITE and not self.allow_writes:
            raise GuardRejected("local writes are disabled")
        if candidate.risk in {Risk.DESTRUCTIVE, Risk.EXTERNAL_SIDE_EFFECT} and not self.allow_destructive:
            raise GuardRejected("destructive or external actions are disabled")
        for precondition in candidate.preconditions:
            if self.precondition_evaluator is None:
                raise GuardRejected("candidate has unchecked preconditions")
            if not self.precondition_evaluator(precondition, observation, candidate):
                raise GuardRejected(f"precondition failed: {precondition}")
        self._validate_paths(candidate)
        self._consumed_observations.add(observation.observation_id)
        return candidate

    def check_fresh(
        self, observation: Observation, candidates: Sequence[ActionCandidate]
    ) -> None:
        """Check GUI evidence without consuming it or approving any action.

        Hosts can refresh after slow planning before spending a policy call.
        Approval repeats this check after the policy returns.
        """
        if any(item.channel == Channel.GUI or item.capability.startswith("desktop.")
               for item in candidates):
            captured_times = [observation.created_at]
            scene = observation.state.get("scene_state")
            if isinstance(scene, dict) and "observed_at" in scene:
                captured_times.append(scene["observed_at"])
            now = time.time()
            for captured_at in captured_times:
                if type(captured_at) not in (int, float):
                    raise GuardRejected("GUI observation capture timestamp is invalid")
                age = now - captured_at
                if not 0 <= age <= self.max_observation_age_s:
                    raise ObservationExpired("GUI observation expired; observe again")

    def _validate_paths(self, candidate: ActionCandidate) -> None:
        def visit(value, name="arguments"):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield from visit(item, key)
            elif isinstance(value, list):
                for item in value:
                    yield from visit(item, name)
            elif isinstance(value, str) and (
                name.endswith("path")
                or name.endswith("_path")
                or name.endswith("_root")
                or name in {"cwd", "workspace", "repo", "directory"}
            ):
                yield name, value

        for name, value in visit(candidate.arguments):
            path = Path(value).resolve()
            if self.allowed_roots and not any(
                path == root or root in path.parents for root in self.allowed_roots
            ):
                raise GuardRejected(f"path argument {name!r} is outside allowed roots")
