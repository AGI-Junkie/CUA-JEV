from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Sequence
from typing import Protocol

import httpx

from .errors import PolicyError
from .models import ActionCandidate, Decision, Observation


class DecisionPolicy(Protocol):
    def choose(self, observation: Observation, candidates: Sequence[ActionCandidate]) -> Decision: ...


def _criteria(candidates: Sequence[ActionCandidate]) -> dict[str, str]:
    if not 1 <= len(candidates) <= 255:
        raise PolicyError("Jev requires between 1 and 255 candidates")
    ids = [candidate.id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise PolicyError("candidate ids must be unique")
    return {
        candidate.id: (
            f"Intent={candidate.intent}. {candidate.description} Channel={candidate.channel}; "
            f"capability={candidate.capability}; "
            f"risk={candidate.risk}; preconditions={list(candidate.preconditions)}."
        )
        for candidate in candidates
    }


class JevPolicy:
    """Strict TypeSafe Jev choice adapter. Credentials never enter traces or errors."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        api_url: str | None = None,
        timeout_s: float = 30,
        retries: int = 4,
        fallback_on_transport: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = api_key or os.getenv("TYPESAFE_API_KEY")
        if not self._key:
            raise PolicyError("TYPESAFE_API_KEY is not set")
        self.model = model or os.getenv("CUA_JEV_MODEL", "jev-1.13.0")
        self.api_url = api_url or os.getenv("CUA_JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
        if not self.api_url.startswith("https://"):
            raise PolicyError("Jev credentials require HTTPS")
        self.timeout_s = timeout_s
        self.retries = retries
        if fallback_on_transport:
            raise PolicyError("Jev rule fallback is disabled; retry or stop when Jev is unavailable")
        timeout = httpx.Timeout(timeout_s, connect=min(5.0, timeout_s))
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None
        self._sleep = sleep
        self.last_exchange: dict = {}
        self.metrics = {"calls": 0, "attempts": 0, "latency_ms": 0.0, "transport_failures": 0}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def choose(self, observation: Observation, candidates: Sequence[ActionCandidate]) -> Decision:
        criteria = _criteria(candidates)
        body = {
            "model": self.model,
            "state": {
                "task": observation.task,
                "subgoal": observation.subgoal,
                "observation_id": observation.observation_id,
                "source": observation.source,
                "state": observation.state,
                "available_actions": [candidate.to_dict() for candidate in candidates],
            },
            "questions": {
                "action": {
                    "type": "choice",
                    "criteria": criteria,
                    "instructions": (
                        "Choose exactly one joint intent-and-execution action that best advances the current "
                        "task. More than one subgoal may currently be legal; use state and recent actions to "
                        "commit to the most useful one. "
                        "Use only the supplied state. Prefer a read-only, directly verifiable action "
                        "when alternatives are equivalent. Respect preconditions and never invent "
                        "an unavailable action."
                    ),
                }
            },
        }
        request_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        started = time.perf_counter()
        self.metrics["calls"] += 1
        result = None
        safe_error = "Jev request failed"
        attempts = 0
        for attempt in range(self.retries + 1):
            attempts += 1
            self.metrics["attempts"] += 1
            try:
                response = self._client.post(
                    self.api_url,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json=body,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    self._sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
                response.raise_for_status()
                result = response.json()
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {401, 403}:
                    safe_error = f"Jev authentication failed (HTTP {status}); check TYPESAFE_API_KEY"
                    break
                safe_error = f"Jev request failed (HTTP {status})"
                if attempt < self.retries:
                    self._sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
            except httpx.ConnectError:
                safe_error = (
                    f"Jev service is temporarily unreachable after {attempts} attempts; retry the run"
                )
                if attempt < self.retries:
                    self._sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
            except httpx.TimeoutException:
                safe_error = f"Jev request timed out after {attempts} attempts; retry the run"
                if attempt < self.retries:
                    self._sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
            except (httpx.HTTPError, ValueError) as exc:
                safe_error = f"Jev request failed ({type(exc).__name__})"
                if attempt < self.retries:
                    self._sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
        latency_ms = (time.perf_counter() - started) * 1000
        self.metrics["latency_ms"] += latency_ms
        if result is None:
            self.metrics["transport_failures"] += 1
            self.last_exchange = {
                "request": body, "request_hash": request_hash, "attempts": attempts,
                "latency_ms": latency_ms, "error": safe_error,
            }
            raise PolicyError(safe_error)
        self.last_exchange = {
            "request": body,
            "request_hash": request_hash,
            "attempts": attempts,
            "response": result,
            "latency_ms": latency_ms,
            "usage": result.get("usage", {}),
        }
        try:
            answer = result["answers"]["action"]
            selected = answer["choice"]
            probabilities = answer["probabilities"]
            confidence = answer["confidence"]
            if selected not in criteria or set(probabilities) != set(criteria):
                raise ValueError
            if not all(type(v) in (float, int) and math.isfinite(v) for v in probabilities.values()):
                raise ValueError
            return Decision(
                observation_id=observation.observation_id,
                candidate_id=selected,
                probabilities={key: float(value) for key, value in probabilities.items()},
                confidence=float(confidence),
                model=str(result.get("model", self.model)),
                latency_ms=latency_ms,
            )
        except (KeyError, TypeError, ValueError):
            raise PolicyError("invalid Jev choice response; no action executed") from None


class RulePolicy:
    """Deterministic baseline: prefer safe channels, then stable candidate order."""

    channel_order = {"mcp": 0, "api": 1, "script": 2, "cli": 3, "gui": 4, "control": 5}
    risk_order = {"read_only": 0, "local_write": 1, "destructive": 2, "external_side_effect": 3}

    def choose(self, observation: Observation, candidates: Sequence[ActionCandidate]) -> Decision:
        _criteria(candidates)
        started = time.perf_counter()
        selected = min(
            candidates,
            key=lambda c: (self.risk_order[str(c.risk)], self.channel_order[str(c.channel)], c.id),
        )
        probabilities = {candidate.id: float(candidate.id == selected.id) for candidate in candidates}
        return Decision(
            observation_id=observation.observation_id,
            candidate_id=selected.id,
            probabilities=probabilities,
            confidence=1.0,
            model="rule-baseline",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
