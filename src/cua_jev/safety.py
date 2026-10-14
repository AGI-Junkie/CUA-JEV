"""Fail-closed run cancellation and immutable physical window evidence."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from .errors import GuardRejected

_policy_admission: ContextVar[Callable[[Callable[[], None]], None] | None] = ContextVar(
    "policy_admission", default=None,
)


@contextmanager
def policy_request_scope(admit: Callable[[Callable[[], None]], None]) -> Iterator[None]:
    """Keep request admission local to the actual calling thread and invocation."""
    context = _policy_admission.set(admit)
    try:
        yield
    finally:
        _policy_admission.reset(context)


def admit_policy_request(record: Callable[[], None]) -> None:
    """Atomically validate and record a cooperative model request/HTTP attempt.

    The admission callback releases its control lock before network I/O. Already
    admitted work may finish after cancellation; no later attempt may be admitted.
    Direct policy callers outside a runtime retain their existing behavior.
    """
    admit = _policy_admission.get()
    if admit is None:
        record()
    else:
        admit(record)


class RuntimeControl:
    """A pause/stop invalidates all outstanding decisions, including after resume.

    Pause waits for an already-dispatched executor to return. It cannot undo input
    already delivered to the OS; no new executor starts after pause returns.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._status = "running"

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def token(self) -> int:
        with self._lock:
            self._check(self._generation)
            return self._generation

    def _check(self, token: int) -> None:
        if self._status != "running" or token != self._generation:
            raise GuardRejected("run paused, stopped, or decision generation invalidated")

    def pause(self) -> None:
        with self._lock:
            self._generation += 1
            if self._status != "stopped":
                self._status = "paused"

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._status = "stopped"

    def resume(self) -> None:
        with self._lock:
            if self._status == "stopped":
                raise GuardRejected("stopped runs cannot resume; create a new runtime")
            self._generation += 1
            self._status = "running"

    @contextmanager
    def dispatch(self, token: int) -> Iterator[None]:
        with self._lock:
            self._check(token)
            yield


@dataclass(frozen=True)
class WindowLease:
    handle: int
    process_id: int
    region: tuple[int, int, int, int]
    dpi: int
    captured_at: float = field(default_factory=time.time)

    def validate(self, current: WindowLease, *, max_age_s: float = 30) -> None:
        age = time.time() - self.captured_at
        if not 0 <= age <= max_age_s:
            raise GuardRejected("window frame expired; observe again")
        if (self.handle, self.process_id, self.region, self.dpi) != (
            current.handle, current.process_id, current.region, current.dpi
        ):
            raise GuardRejected("window identity, geometry, or DPI changed; observe again")

    def validate_point(self, x: float, y: float) -> None:
        left, top, width, height = self.region
        if not (left <= x < left + width and top <= y < top + height):
            raise GuardRejected("selected target lies outside the selected window")
