"""Simulated concurrency/OS evidence tests. No live desktop or model calls."""
import threading
import time

import pytest

from cua_jev.errors import GuardRejected, PolicyError
from cua_jev.guard import ActionGuard
from cua_jev.models import ActionCandidate, Channel, Decision, Observation
from cua_jev.registry import ExecutorRegistry
from cua_jev.runtime import AgentRuntime
from cua_jev.safety import RuntimeControl, WindowLease


def candidate():
    return ActionCandidate("click", Channel.GUI, "desktop.click", "Click safe test target")


def test_stale_gui_frame_is_rejected():
    obs = Observation("test", "click", {}, created_at=time.time() - 60)
    item = candidate()
    decision = Decision(obs.observation_id, item.id, {item.id: 1}, 1, "test", 0)
    with pytest.raises(GuardRejected, match="expired"):
        ActionGuard().approve(obs, decision, [item])


@pytest.mark.parametrize("changed", [
    {"handle": 2}, {"process_id": 22}, {"region": (0, 0, 200, 100)}, {"dpi": 144},
])
def test_window_identity_dpi_geometry_change_rejects(changed):
    values = dict(handle=1, process_id=11, region=(0, 0, 100, 100), dpi=96)
    lease = WindowLease(**values)
    with pytest.raises(GuardRejected, match="changed"):
        lease.validate(WindowLease(**(values | changed)))


def test_window_expiry_and_outside_target_reject():
    lease = WindowLease(1, 11, (0, 0, 100, 100), 96, captured_at=time.time()-40)
    with pytest.raises(GuardRejected, match="expired"):
        lease.validate(WindowLease(1, 11, lease.region, 96))
    with pytest.raises(GuardRejected, match="outside"):
        lease.validate_point(100, 50)


@pytest.mark.parametrize("stop", [False, True])
def test_late_model_result_cannot_dispatch_even_after_resume(stop):
    entered, release = threading.Event(), threading.Event()
    dispatched = []

    class SlowPolicy:
        def choose(self, obs, items):
            entered.set()
            assert release.wait(2)
            return Decision(obs.observation_id, items[0].id, {items[0].id: 1}, 1, "test", 0)

    control = RuntimeControl()
    registry = ExecutorRegistry()
    registry.register(Channel.GUI, lambda *args: dispatched.append(args))
    runtime = AgentRuntime(policy=SlowPolicy(), guard=ActionGuard(), executors=registry, control=control)
    failures = []

    def run():
        try:
            runtime.step(Observation("test", "click", {}), [candidate()])
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(2)
    if stop:
        control.stop()
        with pytest.raises(GuardRejected, match="cannot resume"):
            control.resume()
    else:
        control.pause()
        control.resume()
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert not dispatched
    assert len(failures) == 1 and isinstance(failures[0], GuardRejected)
    assert runtime.metrics["policy_calls"] == 1


def test_api_deadline_does_not_dispatch_late_result():
    release = threading.Event()
    dispatched = []

    class SlowPolicy:
        def choose(self, obs, items):
            release.wait(2)
            return Decision(obs.observation_id, items[0].id, {items[0].id: 1}, 1, "test", 0)

    registry = ExecutorRegistry()
    registry.register(Channel.GUI, lambda *args: dispatched.append(args))
    runtime = AgentRuntime(policy=SlowPolicy(), guard=ActionGuard(), executors=registry,
                           decision_timeout_s=0.01)
    try:
        with pytest.raises(PolicyError, match="deadline"):
            runtime.step(Observation("test", "click", {}), [candidate()])
        assert runtime.metrics["policy_calls"] == runtime.metrics["policy_errors"] == 1
        assert runtime.metrics["policy_latency_ms"] > 0
        assert not dispatched
    finally:
        release.set()


def test_strict_jev_runtime_rejects_rule_policy():
    from cua_jev.policy import RulePolicy
    runtime = AgentRuntime(policy=RulePolicy(), guard=ActionGuard(), executors=ExecutorRegistry(),
                           require_jev=True)
    with pytest.raises(PolicyError, match="Jev API"):
        runtime.step(Observation("test", "click", {}), [candidate()])


def test_timeout_blocks_overlapping_requests_after_pause_resume():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class SlowPolicy:
        def choose(self, obs, items):
            entered.set()
            release.wait(2)
            finished.set()
            return Decision(obs.observation_id, items[0].id, {items[0].id: 1}, 1, "test", 0)

    runtime = AgentRuntime(policy=SlowPolicy(), guard=ActionGuard(), executors=ExecutorRegistry(),
                           decision_timeout_s=0.01)
    try:
        with pytest.raises(PolicyError, match="deadline"):
            runtime.step(Observation("test", "click", {}), [candidate()])
        assert entered.is_set()
        runtime.control.pause()
        runtime.control.resume()
        with pytest.raises(PolicyError, match="still outstanding"):
            runtime.step(Observation("test", "click", {}), [candidate()])
        assert runtime.metrics["policy_calls"] == 1
    finally:
        release.set()
        assert finished.wait(2)


def test_strict_runtime_accepts_known_privacy_wrapper_with_mock_jev_http():
    import httpx

    from cua_jev.open_browser import PublicDecisionPolicy
    from cua_jev.policy import JevPolicy

    def respond(request):
        return httpx.Response(200, json={"answers": {"action": {
            "choice": "click", "probabilities": {"click": 1}, "confidence": 1,
        }}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        policy = JevPolicy(api_key="mock-key", client=client, retries=0)
        runtime = AgentRuntime(policy=PublicDecisionPolicy(policy), guard=ActionGuard(),
                               executors=ExecutorRegistry(), require_jev=True)
        decision = runtime._choose(Observation("test", "click", {}), [candidate()])
        assert decision.candidate_id == "click"
        assert runtime.metrics["policy_calls"] == 1


def test_guarded_click_rechecks_focus_after_pointer_movement():
    from cua_jev.executors.screen import ScreenController

    lease = WindowLease(1, 11, (0, 0, 100, 100), 96)
    clicks = []

    class Pointer:
        def moveTo(self, *args, **kwargs):
            pass

        def click(self):
            clicks.append(True)

    class FakeScreen(ScreenController):
        checks = 0

        def validate_window_lease(self, lease, **kwargs):
            self.checks += 1
            if self.checks == 2:
                raise GuardRejected("selected window is not foreground")

        @staticmethod
        def _modules():
            return Pointer(), None

    with pytest.raises(GuardRejected, match="foreground"):
        FakeScreen().guarded_click_point(lease, 50, 50)
    assert not clicks


@pytest.mark.parametrize("point,region", [
    ((199.5, 100), (0, 0, 200, 200)),
    ((100, 199.5), (0, 0, 200, 200)),
    ((-0.5, -100), (-200, -200, 200, 200)),
])
def test_guarded_click_validates_the_actual_rounded_pixel(point, region):
    from cua_jev.executors.screen import ScreenController

    lease = WindowLease(1, 11, region, 96)
    calls = []

    class Pointer:
        def moveTo(self, x, y, **kwargs):
            calls.append(("move", x, y))

        def click(self):
            calls.append("click")

    class FakeScreen(ScreenController):
        def validate_window_lease(self, lease, **kwargs):
            calls.append("validate")

        @staticmethod
        def _modules():
            calls.append("input modules")
            return Pointer(), None

    with pytest.raises(GuardRejected, match="outside"):
        FakeScreen(pause_s=0).guarded_click_point(lease, *point)
    assert calls == []  # Reject before even moving the pointer beyond the selected window.


def test_guarded_click_runs_visual_validation_after_move_before_click():
    from cua_jev.executors.screen import ScreenController

    lease = WindowLease(1, 11, (0, 0, 200, 200), 96)
    calls = []

    class Pointer:
        def moveTo(self, x, y, **kwargs):
            calls.append(("move", x, y))

        def click(self):
            calls.append("click")

    class FakeScreen(ScreenController):
        def validate_window_lease(self, lease, **kwargs):
            pass

        @staticmethod
        def _modules():
            return Pointer(), None

    def reject_stale_target():
        assert calls == [("move", 50, 50)]
        raise RuntimeError("target changed during pointer movement")

    with pytest.raises(RuntimeError, match="target changed"):
        FakeScreen().guarded_click_point(lease, 50, 50, before_click=reject_stale_target)
    assert calls == [("move", 50, 50)]
