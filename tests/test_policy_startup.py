"""Deterministic request-start races using synthetic policies and mock HTTP only."""
import threading
import time

import httpx
import pytest

from cua_jev.errors import GuardRejected, PolicyError
from cua_jev.guard import ActionGuard
from cua_jev.models import ActionCandidate, Channel, Decision, Observation
from cua_jev.open_browser import PublicDecisionPolicy
from cua_jev.policy import JevPolicy, RulePolicy
from cua_jev.registry import ExecutorRegistry
from cua_jev.runtime import AgentRuntime
from cua_jev.trace import JsonlTrace
from cua_jev.ui.configuration import BudgetLedger, RunLimits


def offered():
    return [ActionCandidate("read", Channel.API, "fixture.read", "Read synthetic fixture")]


def interrupt(control, action):
    if action == "stop":
        control.stop()
    else:
        control.pause()
        if action == "pause_resume":
            control.resume()


def run_in_thread(runtime):
    failures = []

    def run():
        try:
            runtime.step(Observation("fixture", "read", {}), offered())
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, failures


def mock_jev(*, model="jev-fixture-v2", on_request=lambda: None):
    def respond(request):
        on_request()
        return httpx.Response(200, json={"model": model, "answers": {"action": {
            "choice": "read", "probabilities": {"read": 1}, "confidence": 1,
        }}})

    return JevPolicy(api_key="fixture-key", model=model, retries=0,
                     client=httpx.Client(transport=httpx.MockTransport(respond), trust_env=False))


@pytest.mark.parametrize("timeout", [None, 0.25])
@pytest.mark.parametrize("action", ["pause", "pause_resume", "stop"])
def test_cancellation_during_trace_cannot_start_policy_or_charge_budget(timeout, action):
    ledger = BudgetLedger(RunLimits())
    policy = mock_jev(on_request=lambda: ledger.record_model_attempt("jev"))

    class InterruptingTrace(JsonlTrace):
        def append(self, kind, payload):
            super().append(kind, payload)
            if kind == "candidates":
                interrupt(runtime.control, action)

    runtime = AgentRuntime(policy=policy, guard=ActionGuard(), executors=ExecutorRegistry(),
                           trace=InterruptingTrace(), require_jev=True, decision_timeout_s=timeout)
    with pytest.raises(GuardRejected, match="generation invalidated"):
        runtime.step(Observation("fixture", "read", {}), offered())
    assert runtime.metrics["policy_calls"] == 0
    assert policy.metrics["calls"] == policy.metrics["attempts"] == 0
    assert ledger.model_attempts == 0
    assert not runtime.policy_request_pending


@pytest.mark.parametrize("timeout", [None, 0.25])
def test_cancellation_while_acquiring_policy_slot_does_not_start_request(timeout):
    policy = mock_jev()
    runtime = AgentRuntime(policy=policy, guard=ActionGuard(), executors=ExecutorRegistry(),
                           require_jev=True, decision_timeout_s=timeout)
    real_lock = threading.Lock()

    class InterruptingLock:
        def acquire(self, **kwargs):
            runtime.control.pause()
            runtime.control.resume()
            return real_lock.acquire(**kwargs)

        def release(self):
            real_lock.release()

        def locked(self):
            return real_lock.locked()

    runtime._policy_lock = InterruptingLock()
    with pytest.raises(GuardRejected, match="generation invalidated"):
        runtime.step(Observation("fixture", "read", {}), offered())
    assert runtime.metrics["policy_calls"] == policy.metrics["calls"] == 0
    assert not runtime.policy_request_pending


@pytest.mark.parametrize("action", ["pause", "pause_resume", "stop"])
def test_cancellation_after_worker_creation_before_worker_start_skips_http(monkeypatch, action):
    queued, release = threading.Event(), threading.Event()
    real_thread = threading.Thread
    ledger = BudgetLedger(RunLimits())
    policy = mock_jev(on_request=lambda: ledger.record_model_attempt("jev"))
    runtime = AgentRuntime(policy=policy, guard=ActionGuard(), executors=ExecutorRegistry(),
                           require_jev=True, decision_timeout_s=0.5)

    def delayed_thread(*, target, daemon):
        def delayed():
            queued.set()
            assert release.wait(2)
            target()

        return real_thread(target=delayed, daemon=daemon)

    # Retain the unpatched factory for the independent test driver.
    monkeypatch.setattr("cua_jev.runtime.threading.Thread", delayed_thread)
    failures = []

    def run():
        try:
            runtime.step(Observation("fixture", "read", {}), offered())
        except Exception as exc:
            failures.append(exc)

    driver = real_thread(target=run)
    driver.start()
    try:
        assert queued.wait(2)
        interrupt(runtime.control, action)
    finally:
        release.set()
        driver.join(2)
    assert not driver.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], GuardRejected)
    assert runtime.metrics["policy_calls"] == policy.metrics["calls"] == 0
    assert ledger.model_attempts == 0
    assert not runtime.policy_request_pending


@pytest.mark.parametrize("timeout", [None, 0.25])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("model", ["jev-rules-v2", "jev-fallback-trained-v3"])
def test_strict_jev_accepts_valid_model_names_from_mock_api(timeout, wrapped, model):
    policy = mock_jev(model=model)
    runtime = AgentRuntime(policy=PublicDecisionPolicy(policy) if wrapped else policy,
                           guard=ActionGuard(), executors=ExecutorRegistry(), require_jev=True,
                           decision_timeout_s=timeout)
    decision = runtime._choose(Observation("fixture", "read", {}), offered())
    assert decision.model == model
    assert policy.metrics["calls"] == policy.metrics["attempts"] == 1


@pytest.mark.parametrize("wrapped", [False, True])
def test_model_name_cannot_make_rule_policy_pass_strict_provenance(wrapped):
    class PretendingPolicy(RulePolicy):
        def choose(self, obs, items):
            raise AssertionError("strict provenance must reject before calling policy")

    policy = PretendingPolicy()
    runtime = AgentRuntime(policy=PublicDecisionPolicy(policy) if wrapped else policy,
                           guard=ActionGuard(), executors=ExecutorRegistry(), require_jev=True)
    with pytest.raises(PolicyError, match="Jev API"):
        runtime._choose(Observation("fixture", "read", {}), offered())
    assert runtime.metrics["policy_calls"] == 0


@pytest.mark.parametrize("timeout", [None, 0.05])
def test_active_policy_does_not_block_pause_acknowledgement_or_allow_late_action(timeout):
    entered, release = threading.Event(), threading.Event()
    calls, emitted = [], []

    class SlowPolicy:
        def choose(self, obs, items):
            calls.append(True)
            entered.set()
            assert release.wait(2)
            return Decision(obs.observation_id, items[0].id, {items[0].id: 1}, 1, "fixture", 0)

    registry = ExecutorRegistry()
    registry.register(Channel.API, lambda *args: emitted.append(args))
    runtime = AgentRuntime(policy=SlowPolicy(), guard=ActionGuard(), executors=registry,
                           decision_timeout_s=timeout)
    driver, failures = run_in_thread(runtime)
    try:
        assert entered.wait(2)
        started = time.monotonic()
        runtime.control.pause()
        assert time.monotonic() - started < 0.25
        runtime.control.resume()
        if timeout is not None:
            driver.join(1)
            assert not driver.is_alive()
        with pytest.raises(PolicyError, match="still outstanding"):
            runtime.step(Observation("fixture", "read", {}), offered())
    finally:
        release.set()
        driver.join(2)
    assert len(failures) == 1 and isinstance(failures[0], (GuardRejected, PolicyError))
    assert calls == [True] and emitted == []


def test_worker_that_starts_after_deadline_cannot_admit_or_send_http(monkeypatch):
    queued, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_thread = threading.Thread
    policy = mock_jev()
    runtime = AgentRuntime(policy=policy, guard=ActionGuard(), executors=ExecutorRegistry(),
                           require_jev=True, decision_timeout_s=0.01)

    def delayed_thread(*, target, daemon):
        def delayed():
            queued.set()
            assert release.wait(2)
            try:
                target()
            finally:
                finished.set()

        return real_thread(target=delayed, daemon=daemon)

    monkeypatch.setattr("cua_jev.runtime.threading.Thread", delayed_thread)
    try:
        with pytest.raises(PolicyError, match="deadline"):
            runtime.step(Observation("fixture", "read", {}), offered())
        assert queued.is_set() and runtime.policy_request_pending
        with pytest.raises(PolicyError, match="still outstanding"):
            runtime.step(Observation("fixture", "read", {}), offered())
    finally:
        release.set()
        assert finished.wait(2)
    assert runtime.metrics["policy_calls"] == policy.metrics["calls"] == 0
    assert policy.metrics["attempts"] == 0
    assert not runtime.policy_request_pending


@pytest.mark.parametrize("timeout", [None, 0.25])
@pytest.mark.parametrize("action", ["pause", "pause_resume", "stop"])
def test_cancelled_http_request_never_admits_its_retry(timeout, action):
    requests = []
    ledger = BudgetLedger(RunLimits())

    def respond(request):
        ledger.record_model_attempt("jev", retry=len(requests))
        requests.append(request)
        return httpx.Response(503)

    policy = JevPolicy(api_key="fixture-key", retries=2,
                       client=httpx.Client(transport=httpx.MockTransport(respond), trust_env=False),
                       sleep=lambda _: interrupt(runtime.control, action))
    runtime = AgentRuntime(policy=PublicDecisionPolicy(policy), guard=ActionGuard(),
                           executors=ExecutorRegistry(), require_jev=True, decision_timeout_s=timeout)
    with pytest.raises(GuardRejected, match="generation invalidated"):
        runtime.step(Observation("fixture", "read", {}), offered())
    assert runtime.metrics["policy_calls"] == policy.metrics["calls"] == 1
    assert len(requests) == policy.metrics["attempts"] == ledger.model_attempts == 1
    assert not runtime.policy_request_pending


def test_timed_out_http_request_never_admits_retry(monkeypatch):
    sleeping, release, finished = threading.Event(), threading.Event(), threading.Event()
    requests = []
    clock = [0.0]
    # Advance expiry only after the initial HTTP attempt has entered. This keeps
    # the retry regression deterministic even on a heavily loaded CI runner.
    monkeypatch.setattr("cua_jev.runtime.time.perf_counter", lambda: clock[0])

    def respond(request):
        requests.append(request)
        return httpx.Response(503)

    def sleep(_):
        sleeping.set()
        assert release.wait(2)

    class FinishingPolicy(JevPolicy):
        def choose(self, *args):
            try:
                return super().choose(*args)
            finally:
                finished.set()

    policy = FinishingPolicy(api_key="fixture-key", retries=2, sleep=sleep,
                            client=httpx.Client(transport=httpx.MockTransport(respond), trust_env=False))
    runtime = AgentRuntime(policy=policy, guard=ActionGuard(), executors=ExecutorRegistry(),
                           require_jev=True, decision_timeout_s=0.05)
    driver, failures = run_in_thread(runtime)
    try:
        assert sleeping.wait(2)
        clock[0] = 1.0
        driver.join(2)
        assert not driver.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], PolicyError)
        assert "deadline" in str(failures[0])
    finally:
        release.set()
        assert finished.wait(2)
    assert len(requests) == policy.metrics["attempts"] == 1


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 10**400, "1"])
def test_invalid_policy_deadlines_rejected_before_start(value):
    with pytest.raises(ValueError, match="decision timeout"):
        AgentRuntime(policy=RulePolicy(), guard=ActionGuard(), executors=ExecutorRegistry(),
                     decision_timeout_s=value)


def test_reentrant_start_callback_cannot_admit_after_it_invalidates_generation():
    policy = mock_jev()
    runtime = AgentRuntime(policy=policy, guard=ActionGuard(), executors=ExecutorRegistry())

    def cancel():
        runtime.control.pause()
        runtime.control.resume()

    with pytest.raises(GuardRejected, match="generation invalidated"):
        runtime._choose(Observation("fixture", "read", {}), offered(), before_request=cancel)
    assert runtime.metrics["policy_calls"] == policy.metrics["calls"] == 0
    assert not runtime.policy_request_pending


def test_reentrant_final_dispatch_callback_cannot_execute_after_cancellation():
    emitted, checks = [], []
    registry = ExecutorRegistry()
    registry.register(Channel.API, lambda *args: emitted.append("physical input"))
    runtime = AgentRuntime(policy=RulePolicy(), guard=ActionGuard(), executors=registry)

    def cancel_final_dispatch():
        checks.append(True)
        if len(checks) == 3:  # Initial check, request admission, final action dispatch.
            runtime.control.pause()
            runtime.control.resume()

    with pytest.raises(GuardRejected, match="generation invalidated"):
        runtime.step(Observation("fixture", "read", {}), offered(),
                     before_dispatch=cancel_final_dispatch)
    assert len(checks) == 3 and emitted == []
