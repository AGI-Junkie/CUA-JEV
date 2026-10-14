from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .errors import GuardRejected, PolicyError
from .guard import ActionGuard
from .models import ActionCandidate, ActionReceipt, Decision, JevDecision, Observation, Verification
from .policy import DecisionPolicy, JevPolicy
from .registry import ExecutorRegistry
from .safety import RuntimeControl, policy_request_scope
from .trace import JsonlTrace
from .verify import VerifierRegistry


@dataclass(frozen=True)
class StepResult:
    decision: Decision
    receipt: ActionReceipt
    verification: Verification


class AgentRuntime:
    def __init__(
        self,
        *,
        policy: DecisionPolicy,
        guard: ActionGuard,
        executors: ExecutorRegistry,
        verifiers: VerifierRegistry | None = None,
        trace: JsonlTrace | None = None,
        control: RuntimeControl | None = None,
        require_jev: bool = False,
        decision_timeout_s: float | None = None,
    ) -> None:
        self.policy = policy
        self.guard = guard
        self.executors = executors
        self.verifiers = verifiers or VerifierRegistry()
        self.trace = trace or JsonlTrace()
        self.control = control or RuntimeControl()
        self.require_jev = require_jev
        self.last_jev_decision: JevDecision | None = None
        if decision_timeout_s is not None and (
            type(decision_timeout_s) not in (int, float)
            or not 0 < decision_timeout_s <= threading.TIMEOUT_MAX
        ):
            raise ValueError("decision timeout must be finite, positive, and within platform limits")
        self.decision_timeout_s = decision_timeout_s
        self._policy_lock = threading.Lock()
        self.metrics = {"policy_calls": 0, "policy_errors": 0, "policy_latency_ms": 0.0}

    @property
    def policy_request_pending(self) -> bool:
        return self._policy_lock.locked()

    def _choose(self, observation, candidates, *, expected_generation=None, before_request=None):
        from .open_browser import PublicDecisionPolicy

        generation = self.control.token() if expected_generation is None else expected_generation
        policy = self.policy.inner if isinstance(self.policy, PublicDecisionPolicy) else self.policy
        if self.require_jev and not isinstance(policy, JevPolicy):
            raise PolicyError("this run requires the Jev API policy")
        if not self._policy_lock.acquire(blocking=False):
            raise PolicyError("previous policy request still outstanding; wait before retrying")
        worker_started = False
        started = time.perf_counter()
        deadline = started + self.decision_timeout_s if self.decision_timeout_s is not None else None

        def admit(record):
            # This is the request's linearization point, including for delayed
            # workers and HTTP retries. Never retain this lock during choose/I/O.
            with self.control.dispatch(generation):
                if before_request is not None:
                    before_request()
                # A reentrant callback may itself invalidate this generation.
                with self.control.dispatch(generation):
                    if deadline is not None and time.perf_counter() >= deadline:
                        raise PolicyError("decision API deadline exceeded; no action executed")
                    record()

        def record_call():
            self.metrics["policy_calls"] += 1

        def invoke():
            # Admission belongs to the calling worker, not its creator. A pause
            # while Thread.start is queued must not launch or count a new call.
            with policy_request_scope(admit):
                admit(record_call)
                return self.policy.choose(observation, candidates)

        try:
            if self.decision_timeout_s is None:
                decision = invoke()
            else:
                results = queue.Queue(maxsize=1)

                def choose():
                    try:
                        results.put((True, invoke()))
                    except Exception as exc:
                        results.put((False, exc))
                    finally:
                        self._policy_lock.release()

                threading.Thread(target=choose, daemon=True).start()
                worker_started = True
                try:
                    success, value = results.get(timeout=max(0, deadline - time.perf_counter()))
                except queue.Empty:
                    raise PolicyError("decision API deadline exceeded; no action executed") from None
                if not success:
                    raise value
                decision = value
                if time.perf_counter() >= deadline:
                    raise PolicyError("decision API deadline exceeded; no action executed")
            return decision
        except Exception:
            self.metrics["policy_errors"] += 1
            raise
        finally:
            if not worker_started:
                self._policy_lock.release()
            self.metrics["policy_latency_ms"] += (time.perf_counter() - started) * 1000

    def step(
        self, observation: Observation, candidates: Sequence[ActionCandidate], *,
        expected_generation: int | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> StepResult:
        generation = self.control.token() if expected_generation is None else expected_generation
        with self.control.dispatch(generation):
            if before_dispatch is not None:
                before_dispatch()
        self.trace.append("observation", observation.to_dict())
        self.trace.append("candidates", {"items": [candidate.to_dict() for candidate in candidates]})
        decision = self._choose(
            observation, candidates, expected_generation=generation, before_request=before_dispatch,
        )
        self.trace.append("decision", decision.to_dict())
        exchange = getattr(self.policy, "last_exchange", None)
        if exchange:
            self.trace.append("policy_exchange", exchange)
        if self.require_jev:
            self.last_jev_decision = JevDecision.bind(
                decision, observation, tuple(candidates), (exchange or {}).get("request_hash")
            )
            self.trace.append("jev_decision", self.last_jev_decision.to_dict())
        candidate = next((item for item in candidates if item.id == decision.candidate_id), None)
        self.trace.append(
            "commitment",
            {
                "candidate_id": decision.candidate_id,
                "intent": candidate.intent if candidate else "unknown",
                "channel": str(candidate.channel) if candidate else "unknown",
                "capability": candidate.capability if candidate else "unknown",
            },
        )
        try:
            candidate = self.guard.approve(observation, decision, candidates)
        except GuardRejected as exc:
            self.trace.append(
                "guard",
                {"approved": False, "candidate_id": decision.candidate_id, "reason": str(exc)},
            )
            raise
        self.trace.append(
            "guard",
            {
                "approved": True,
                "candidate_id": candidate.id,
                "checks": ["fresh_observation", "offered_candidate", "risk", "paths", "preconditions"],
            },
        )
        with self.control.dispatch(generation):
            if before_dispatch is not None:
                before_dispatch()
            with self.control.dispatch(generation):
                receipt = self.executors.execute(candidate, observation.observation_id, decision.decision_id)
        self.trace.append("receipt", receipt.to_dict())
        verification = self.verifiers.verify(candidate, receipt)
        self.trace.append("verification", verification.to_dict())
        return StepResult(decision, receipt, verification)
