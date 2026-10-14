"""Synthetic GUI timing/layout evidence and explicit local mock Jev HTTP only."""

from types import SimpleNamespace

import httpx
import pytest

from cua_jev.episode import EpisodeConfig, EpisodeRunner, EpisodeStatus, Evaluation
from cua_jev.errors import GuardRejected
from cua_jev.guard import ActionGuard
from cua_jev.models import ActionCandidate, ActionReceipt, Channel, Decision, Observation
from cua_jev.open_desktop import DesktopPlan, OpenDesktopTask
from cua_jev.policy import JevPolicy
from cua_jev.registry import ExecutorRegistry
from cua_jev.runtime import AgentRuntime
from cua_jev.safety import RuntimeControl, WindowLease
from cua_jev.verify import VerifierRegistry


class Clock:
    value = 1000.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    timing = SimpleNamespace(time=clock.now, monotonic=clock.now, sleep=lambda _: None)
    for module in ("guard", "episode", "open_desktop", "safety"):
        monkeypatch.setattr(f"cua_jev.{module}.time", timing)
    # Observation's default factory retains the real time function; inject the
    # controlled capture time without modifying the application's timestamps.
    monkeypatch.setattr(
        "cua_jev.open_desktop.Observation",
        lambda **values: Observation(**values, created_at=clock.now()),
    )
    return clock


class Button:
    def __init__(self):
        self.left = 10
        self.element_info = SimpleNamespace(
            name="Finish", control_type="Button", automation_id="finish", is_password=False,
        )

    def window_text(self):
        return "Finish"

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def rectangle(self):
        return SimpleNamespace(left=self.left, top=20, right=self.left + 80, bottom=60)


class Window:
    handle = 42

    def __init__(self):
        self.title = "Synthetic task"
        self.button = Button()

    def window_text(self):
        return self.title

    def descendants(self):
        return [self.button]


class Screen:
    def __init__(self, clock, window):
        self.clock, self.window = clock, window
        self.leases, self.clicks = [], []

    def capture_window_lease(self, handle):
        lease = WindowLease(handle, 123, (0, 0, 300, 300), 96, self.clock.now())
        self.leases.append(lease)
        return lease

    def validate_window_lease(self, lease):
        lease.validate(WindowLease(42, 123, (0, 0, 300, 300), 96, self.clock.now()))

    def guarded_click_point(self, lease, x, y):
        self.validate_window_lease(lease)
        assert x == self.window.button.left + 40
        self.clicks.append((x, y, lease.captured_at))
        self.window.title = "Done"


def test_slow_planner_reobserves_rebinds_and_renews_lease_before_mock_jev(clock):
    window = Window()
    screen = Screen(clock, window)

    class SlowPlanner:
        calls = 0

        def plan(self, goal, snapshot, recent):
            self.calls += 1
            plan = DesktopPlan.from_dict({
                "subgoal": "Finish", "options": [{"ref": "c0", "operation": "click"}],
                "success": {"kind": "window_title_contains", "value": "Done"},
            }, snapshot)
            clock.advance(40)  # Allowed planner timing exceeds the 30-second guard.
            window.button.left = 140  # Live geometry must be rebound as well.
            return plan

    planner = SlowPlanner()
    task = OpenDesktopTask(
        goal="Finish synthetic task", window_title_re="Synthetic", planner=planner,
        window=window, screen=screen, allow_button_actions=True,
    )
    choices = []

    def respond(request):
        choices.append(request)
        return httpx.Response(200, json={"answers": {"action": {
            "choice": "option_0_gui", "confidence": 1, "probabilities": {"option_0_gui": 1},
        }}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def factory(environment):
            registry = ExecutorRegistry()
            registry.register(Channel.GUI, environment)
            verifiers = VerifierRegistry()
            environment.register_verifiers(verifiers)
            return AgentRuntime(
                policy=JevPolicy(api_key="non-secret-mock", client=client, retries=0),
                guard=ActionGuard(allow_destructive=True), executors=registry,
                verifiers=verifiers, require_jev=True,
            )

        result = EpisodeRunner(factory, EpisodeConfig(max_steps=1)).run(task)
    assert result.status == EpisodeStatus.SUCCESS, result.reason
    assert planner.calls == len(choices) == len(result.steps) == 1
    assert len(screen.leases) == 2
    assert screen.clicks == [(180, 40, 1040)]
    assert result.steps[0].verification.passed


class GuiTask:
    name = "synthetic-refresh"

    def __init__(self, clock, *, planner_delay=0, empty_after_refresh=False):
        self.clock = clock
        self.planner_delay = planner_delay
        self.empty_after_refresh = empty_after_refresh
        self.observations = []
        self.resets = 0
        self.planned = False

    def reset(self):
        self.resets += 1

    def observe(self, history):
        observation = Observation(
            "synthetic task", "click", {"frame": len(self.observations)}, created_at=self.clock.now(),
        )
        self.observations.append(observation)
        return observation

    def candidates(self, observation, history):
        if not self.planned:
            self.planned = True
            self.clock.advance(self.planner_delay)
        if self.empty_after_refresh and len(self.observations) > 1:
            return ()
        return (ActionCandidate(
            "click", Channel.GUI, "desktop.click", "Synthetic click",
            {"fresh_frame": observation.state["frame"]},
        ),)

    def evaluate(self, observation, candidate, receipt, verification):
        return Evaluation(True, True, "verified synthetic result")


class SimulatedPolicy:
    def __init__(self, clock, delays=(), on_choose=None):
        self.clock, self.delays, self.on_choose = clock, list(delays), on_choose
        self.observations = []

    def choose(self, observation, candidates):
        self.observations.append(observation)
        if self.delays:
            self.clock.advance(self.delays.pop(0))
        if self.on_choose:
            self.on_choose()
        return Decision(observation.observation_id, "click", {"click": 1}, 1, "simulated-local", 0)


def synthetic_runtime(clock, policy, *, guard=None, control=None):
    dispatched = []
    registry = ExecutorRegistry()

    def execute(candidate, observation_id, decision_id):
        dispatched.append((candidate, observation_id, decision_id))
        return ActionReceipt(
            observation_id, decision_id, candidate.id, candidate.channel, candidate.capability,
            True, clock.now(), clock.now(),
        )

    registry.register(Channel.GUI, execute)
    runtime = AgentRuntime(
        policy=policy, guard=guard or ActionGuard(), executors=registry, control=control,
    )
    return runtime, dispatched


def test_late_policy_choice_is_discarded_and_fresh_choice_counts_against_same_budget(clock):
    policy = SimulatedPolicy(clock, [31, 0])
    runtime, dispatched = synthetic_runtime(clock, policy)
    task = GuiTask(clock)
    result = EpisodeRunner(lambda: runtime, EpisodeConfig(max_steps=1)).run(task)
    assert result.status == EpisodeStatus.SUCCESS, result.reason
    assert task.resets == 1
    assert len(task.observations) == runtime.metrics["policy_calls"] == 2
    assert len(dispatched) == len(result.steps) == 1
    assert dispatched[0][1] == task.observations[1].observation_id
    assert dispatched[0][0].arguments["fresh_frame"] == 1
    assert task.observations[0].observation_id != task.observations[1].observation_id


def test_repeated_late_choices_exhaust_global_refresh_budget_without_input(clock):
    policy = SimulatedPolicy(clock, [31, 31, 31])
    runtime, dispatched = synthetic_runtime(clock, policy)
    task = GuiTask(clock)
    result = EpisodeRunner(
        lambda: runtime, EpisodeConfig(max_steps=1, max_observation_refreshes=2),
    ).run(task)
    assert result.status == EpisodeStatus.GUARD_REJECTED
    assert "refresh budget exhausted" in result.reason
    assert len(task.observations) == runtime.metrics["policy_calls"] == 3
    assert not dispatched and not result.steps
    assert len([event for event in runtime.trace.events if event["kind"] == "observation_refresh"]) == 2


def test_reobserve_without_legal_rebound_target_never_calls_policy_or_inputs(clock):
    runtime, dispatched = synthetic_runtime(clock, SimulatedPolicy(clock))
    task = GuiTask(clock, planner_delay=40, empty_after_refresh=True)
    result = EpisodeRunner(lambda: runtime).run(task)
    assert result.status == EpisodeStatus.TASK_FAILURE
    assert len(task.observations) == 2
    assert not dispatched and runtime.metrics["policy_calls"] == 0


@pytest.mark.parametrize("at_policy", [False, True])
def test_wall_timeout_after_slow_model_blocks_refresh_and_input(clock, at_policy):
    policy = SimulatedPolicy(clock, [40] if at_policy else [])
    runtime, dispatched = synthetic_runtime(clock, policy)
    task = GuiTask(clock, planner_delay=0 if at_policy else 40)
    result = EpisodeRunner(lambda: runtime, EpisodeConfig(timeout_s=35)).run(task)
    assert result.status == EpisodeStatus.TIMEOUT
    assert len(task.observations) == 1
    assert runtime.metrics["policy_calls"] == int(at_policy)
    assert not dispatched


@pytest.mark.parametrize("interrupt", ["pause", "pause_resume", "stop"])
def test_late_planner_interruption_cannot_be_revived_by_refresh(clock, interrupt):
    control = RuntimeControl()
    runtime, dispatched = synthetic_runtime(clock, SimulatedPolicy(clock), control=control)

    class InterruptedTask(GuiTask):
        def candidates(self, observation, history):
            candidates = super().candidates(observation, history)
            if interrupt == "stop":
                control.stop()
            else:
                control.pause()
                if interrupt == "pause_resume":
                    control.resume()
            return candidates

    task = InterruptedTask(clock, planner_delay=40)
    result = EpisodeRunner(lambda: runtime).run(task)
    assert result.status == EpisodeStatus.GUARD_REJECTED
    assert "generation invalidated" in result.reason
    assert len(task.observations) == 1
    assert not dispatched and runtime.metrics["policy_calls"] == 0


def test_nonfreshness_guard_rejection_is_not_retried(clock):
    candidate = ActionCandidate(
        "click", Channel.GUI, "desktop.click", "Synthetic click", requires_confirmation=True,
    )

    class ConfirmTask(GuiTask):
        def candidates(self, observation, history):
            return (candidate,)

    runtime, dispatched = synthetic_runtime(clock, SimulatedPolicy(clock))
    task = ConfirmTask(clock)
    result = EpisodeRunner(lambda: runtime).run(task)
    assert result.status == EpisodeStatus.GUARD_REJECTED
    assert "explicit confirmation" in result.reason
    assert len(task.observations) == runtime.metrics["policy_calls"] == 1
    assert not dispatched


def test_runtime_honors_generation_from_before_planning(clock):
    control = RuntimeControl()
    generation = control.token()
    runtime, dispatched = synthetic_runtime(clock, SimulatedPolicy(clock), control=control)
    control.pause()
    control.resume()
    with pytest.raises(GuardRejected, match="generation invalidated"):
        runtime.step(
            Observation("test", "click", {}, created_at=clock.now()),
            [ActionCandidate("click", Channel.GUI, "desktop.click", "Synthetic click")],
            expected_generation=generation,
        )
    assert not dispatched and runtime.metrics["policy_calls"] == 0


def test_guard_uses_physical_capture_age_even_when_observation_was_just_created(clock):
    from cua_jev.errors import ObservationExpired

    candidate = ActionCandidate("click", Channel.GUI, "desktop.click", "Synthetic click")
    observation = Observation(
        "test", "click", {"scene_state": {"observed_at": clock.now() - 40}},
        created_at=clock.now(),
    )
    with pytest.raises(ObservationExpired, match="expired"):
        ActionGuard().check_fresh(observation, [candidate])


def test_physical_capture_expiry_after_vision_refreshes_with_global_cap(clock):
    runtime, dispatched = synthetic_runtime(clock, SimulatedPolicy(clock))

    class SlowVisionTask(GuiTask):
        def observe(self, history):
            observation = super().observe(history)
            # A slow grounder finishes after capture; construction time alone
            # must not make its stale physical evidence eligible for input.
            return Observation(
                observation.task, observation.subgoal,
                {**observation.state, "scene_state": {"observed_at": self.clock.now() - 40}},
                created_at=self.clock.now(),
            )

    task = SlowVisionTask(clock)
    result = EpisodeRunner(lambda: runtime).run(task)
    assert result.status == EpisodeStatus.GUARD_REJECTED
    assert "refresh budget exhausted" in result.reason
    assert len(task.observations) == 3
    assert runtime.metrics["policy_calls"] == 0 and not dispatched
