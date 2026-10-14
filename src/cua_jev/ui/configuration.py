"""Local nonsecret configuration and strict, per-session execution budgets.

Keys are deliberately process-memory only. This module never probes endpoints or
reads environment credentials. A snapshot cannot enable live paid execution.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

ROLES = ("planner", "vision", "jev")


class ConfigurationError(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RunLimits:
    max_steps: int = 20
    max_wall_seconds: float = 60
    max_model_attempts: int = 20
    max_retries: int = 2
    decision_timeout_seconds: float = 30

    def __post_init__(self) -> None:
        for name, low, high, integer in (
            ("max_steps", 1, 100, True),
            ("max_wall_seconds", 1, 600, False),
            ("max_model_attempts", 1, 20, True),
            ("max_retries", 0, 2, True),
            ("decision_timeout_seconds", 0.1, 30, False),
        ):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low <= value <= high
                    or (integer and not isinstance(value, int))):
                raise ConfigurationError(f"Invalid limit: {name}")


@dataclass(frozen=True)
class ModelRoleConfig:
    name: str = ""
    endpoint: str = ""
    api_key: str = field(default="", repr=False, compare=False)
    estimated_dollars_per_attempt: float | None = None

    def public(self) -> dict:
        return {
            "name": self.name, "endpoint": self.endpoint,
            "key_configured": bool(self.api_key), "verified": False,
            "status": "configured_unverified" if self.name and self.endpoint else "incomplete",
            "estimated_dollars_per_attempt": self.estimated_dollars_per_attempt,
        }


@dataclass(frozen=True)
class ConfigurationSnapshot:
    roles: Mapping[str, ModelRoleConfig]
    limits: RunLimits
    mode: str = "controlled_mock"
    cost_ceiling_dollars: float | None = None

    def public(self) -> dict:
        return {
            "mode": self.mode, "live_execution_enabled": False,
            "roles": {role: value.public() for role, value in self.roles.items()},
            "limits": asdict(self.limits),
            "cost": {
                "ceiling_dollars": self.cost_ceiling_dollars,
                "ceiling_enforced": False,
                "reason": "Provider billing is unverified; dollar limits cannot be enforced.",
                "estimation": "user_supplied_per_attempt_only",
            },
            "key_storage": "process_memory_only; blank preserves; clear_api_key removes",
        }


def _amount(value: object) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or value > 1_000_000):
        raise ConfigurationError("Invalid dollar amount")
    return float(value)


def _endpoint(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ConfigurationError("Invalid endpoint")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        del port
        if (not hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(char.isspace() for char in value)
                or "\\" in value):
            raise ValueError
        loopback = hostname.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
    except ValueError:
        raise ConfigurationError("Endpoint requires HTTPS, or loopback HTTP for controlled mock") from None
    return value


class ConfigurationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._snapshot = ConfigurationSnapshot(
            MappingProxyType({role: ModelRoleConfig(
                endpoint="https://api.typesafe.ai/v1/systemone" if role == "jev" else ""
            ) for role in ROLES}), RunLimits(),
        )
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                # Credentials on disk are never imported, including hand-edited files.
                for config in data.get("roles", {}).values():
                    if isinstance(config, dict):
                        config.pop("api_key", None)
                        config.pop("clear_api_key", None)
                self._apply(data, persist=False)
            except (ValueError, TypeError, AttributeError, OSError):
                raise ConfigurationError("Cannot load local nonsecret configuration") from None

    def snapshot(self) -> ConfigurationSnapshot:
        with self._lock:
            return self._snapshot

    def public(self) -> dict:
        return self.snapshot().public()

    def update(self, payload: dict) -> dict:
        with self._lock:
            self._apply(payload, persist=True)
            return self._snapshot.public()

    def _apply(self, payload: dict, *, persist: bool) -> None:
        allowed = {"roles", "limits", "mode", "cost_ceiling_dollars"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ConfigurationError("Unsupported configuration fields")
        if payload.get("mode", "controlled_mock") != "controlled_mock":
            raise ConfigurationError("Live execution is disabled; controlled_mock is the supported mode")
        current = self._snapshot
        roles = dict(current.roles)
        updates = payload.get("roles", {})
        if not isinstance(updates, dict) or set(updates) - set(ROLES):
            raise ConfigurationError("Invalid model roles")
        for role, config in updates.items():
            if not isinstance(config, dict) or set(config) - {
                "name", "endpoint", "api_key", "clear_api_key", "estimated_dollars_per_attempt"
            }:
                raise ConfigurationError("Invalid model role fields")
            old = roles[role]
            name = config.get("name", old.name)
            key = config.get("api_key", "")
            clear = config.get("clear_api_key", False)
            if (not isinstance(name, str) or len(name) > 200 or any(ord(c) < 32 for c in name)
                    or not isinstance(key, str) or len(key) > 8192 or not isinstance(clear, bool)
                    or any(ord(c) < 32 for c in key) or (clear and key)):
                raise ConfigurationError("Invalid model role values")
            endpoint = _endpoint(config.get("endpoint", old.endpoint))
            if role == "jev" and endpoint and urlsplit(endpoint).scheme != "https":
                raise ConfigurationError("Jev endpoint requires HTTPS")
            roles[role] = ModelRoleConfig(
                name, endpoint,
                "" if clear else key or old.api_key,
                _amount(config.get("estimated_dollars_per_attempt", old.estimated_dollars_per_attempt)),
            )
        limits = payload.get("limits", {})
        if not isinstance(limits, dict) or set(limits) - set(asdict(current.limits)):
            raise ConfigurationError("Invalid limits fields")
        snapshot = ConfigurationSnapshot(
            MappingProxyType(roles), RunLimits(**(asdict(current.limits) | limits)),
            cost_ceiling_dollars=_amount(payload.get("cost_ceiling_dollars", current.cost_ceiling_dollars)),
        )
        if persist:
            self._save(snapshot)
        self._snapshot = snapshot

    def _save(self, snapshot: ConfigurationSnapshot) -> None:
        data = {
            "mode": snapshot.mode, "limits": asdict(snapshot.limits),
            "cost_ceiling_dollars": snapshot.cost_ceiling_dollars,
            "roles": {role: {"name": config.name, "endpoint": config.endpoint,
                             "estimated_dollars_per_attempt": config.estimated_dollars_per_attempt}
                      for role, config in snapshot.roles.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".configuration-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class BudgetLedger:
    """Charge *before* each dispatch; retries are transport attempts too."""
    def __init__(self, limits: RunLimits, *, clock: Callable[[], float] = time.monotonic,
                 cancelled: Callable[[], bool] = lambda: False,
                 roles: Mapping[str, ModelRoleConfig] | None = None):
        self.limits = limits
        self._clock, self._cancelled = clock, cancelled
        self._started = clock()
        self._lock = threading.RLock()
        self.steps = 0
        self.model_attempts = 0
        self.attempts_by_role: dict[str, int] = {}
        self._roles = roles or {}

    def check(self) -> None:
        if self._cancelled():
            raise BudgetExceeded("Session cancelled or paused")
        if self._clock() - self._started >= self.limits.max_wall_seconds:
            raise BudgetExceeded("Session wall time exhausted")

    def record_model_attempt(self, role: str, *, retry: int = 0) -> None:
        with self._lock:
            self.check()
            if role not in ROLES:
                raise BudgetExceeded("Unknown model role")
            if (isinstance(retry, bool) or not isinstance(retry, int)
                    or retry < 0 or retry > self.limits.max_retries):
                raise BudgetExceeded("Model retry budget exhausted")
            if self.model_attempts >= self.limits.max_model_attempts:
                raise BudgetExceeded("Model attempt budget exhausted")
            self.model_attempts += 1
            self.attempts_by_role[role] = self.attempts_by_role.get(role, 0) + 1

    def record_step(self) -> None:
        with self._lock:
            self.check()
            if self.steps >= self.limits.max_steps:
                raise BudgetExceeded("Step budget exhausted")
            self.steps += 1

    def public(self) -> dict:
        with self._lock:
            known = all(self._roles.get(role) is not None
                        and self._roles[role].estimated_dollars_per_attempt is not None
                        for role in self.attempts_by_role)
            estimate = sum(self._roles[role].estimated_dollars_per_attempt * count
                           for role, count in self.attempts_by_role.items()) if known else None
            return {"steps": self.steps, "model_attempts": self.model_attempts,
                    "attempts_by_role": dict(self.attempts_by_role),
                    "elapsed_seconds": max(0, self._clock() - self._started),
                    "estimated_cost_dollars": estimate,
                    "cost_status": "user_estimate" if known else "unknown",
                    "cost_ceiling_enforced": False}
