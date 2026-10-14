"""Synthetic unchanged desktop observations and local mock Jev responses only."""

import copy
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from cua_jev.episode import EpisodeConfig, EpisodeRunner, EpisodeStatus
from cua_jev.errors import GuardRejected, ObservationExpired
from cua_jev.guard import ActionGuard
from cua_jev.models import (
    ActionCandidate,
    Channel,
    Decision,
    JevDecision,
    Observation,
    stable_scene_fingerprint,
    state_fingerprint,
)
from cua_jev.open_desktop import DesktopPlan, OpenDesktopTask
from cua_jev.policy import JevPolicy
from cua_jev.registry import ExecutorRegistry
from cua_jev.runtime import AgentRuntime
from cua_jev.safety import WindowLease
from cua_jev.verify import VerifierRegistry


def test_unchanged_desktop_stops_repeated_unverified_side_effects_at_limit():
    class Button:
        element_info = SimpleNamespace(
            name="Finish", control_type="Button", automation_id="finish", is_password=False,
        )
        clicks = 0

        def window_text(self):
            return "Finish"

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def rectangle(self):
            return SimpleNamespace(left=10, top=20, right=90, bottom=60)

        def invoke(self):
            # Input was dispatched, but no independent UI effect followed.
            self.clicks += 1

    class Window:
        handle = 42
        button = Button()

        def window_text(self):
            return "Synthetic task"

        def descendants(self):
            return [self.button]

    class Screen:
        def capture_window_lease(self, handle):
            return WindowLease(handle, 123, (0, 0, 300, 300), 96)

        def validate_window_lease(self, lease):
            lease.validate(self.capture_window_lease(lease.handle))

    class Planner:
        calls = 0

        def plan(self, goal, snapshot, recent):
            self.calls += 1
            return DesktopPlan.from_dict({
                "subgoal": f"Finish attempt {self.calls}",
                "options": [{"ref": "c0", "operation": "click"}],
                "success": {"kind": "window_title_contains", "value": "Done"},
            }, snapshot)

    window = Window()
    environment = OpenDesktopTask(
        goal="Finish synthetic task", window_title_re="Synthetic", planner=Planner(),
        window=window, screen=Screen(), allow_button_actions=True,
    )
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"answers": {"action": {
            "choice": "option_0_uia", "confidence": 1,
            "probabilities": {"option_0_uia": 1, "option_0_gui": 0},
        }}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def runtime_factory(task):
            executors = ExecutorRegistry()
            executors.register(Channel.API, task)
            verifiers = VerifierRegistry()
            task.register_verifiers(verifiers)
            return AgentRuntime(
                policy=JevPolicy(api_key="non-secret-mock", client=client, retries=0),
                guard=ActionGuard(allow_destructive=True), executors=executors,
                verifiers=verifiers, require_jev=True,
            )

        result = EpisodeRunner(runtime_factory, EpisodeConfig(
            max_steps=8, unchanged_limit=3, repeated_action_limit=3,
        )).run(environment)

    assert result.status == EpisodeStatus.STUCK, result.reason
    assert len(result.steps) == len(requests) == window.button.clicks == 3
    assert all(step.receipt.success and not step.verification.passed for step in result.steps)
    contracts = [json.loads(request.content)["state"]["state"]["plan_contract"] for request in requests]
    assert contracts[0] is None
    assert contracts[1]["contract_id"] != contracts[2]["contract_id"]
    assert contracts[1]["subgoal"] != contracts[2]["subgoal"]


def desktop_observation():
    return Observation("Synthetic task", "Finish", {
        "window_title": "Synthetic task",
        "text": "Ready",
        "controls": [{"name": "Finish", "rectangle": [10, 20, 90, 60]}],
        "edit_values": ["initial"],
        "visual_image_hash": "original-pixels",
        "observed_at": "application observation time",
        "captured_at": "application capture time",
        "application": {"observed_at": 123, "captured_at": 456, "plan_contract": "user data"},
        "scene_state": {
            "window_handle": 42,
            "fingerprint": "physical-scene-fingerprint",
            "observed_at": time.time(),
            "captured_at": "unrelated scene property",
            "window_evidence": {
                "handle": 42, "process_id": 123, "region": [0, 0, 300, 300],
                "dpi": 96, "captured_at": time.time(), "foreground_checked": True,
                "observed_at": "application evidence time",
                "application": {"captured_at": 456},
            },
        },
        "plan_contract": {"contract_id": "plan-one", "subgoal": "Finish", "completed_options": []},
    }, source="open-desktop")


def test_stable_scene_ignores_only_known_capture_and_planning_bookkeeping_without_mutation():
    original = desktop_observation()
    preserved = copy.deepcopy(original.to_dict())
    state = copy.deepcopy(original.state)
    state["scene_state"]["observed_at"] += 1
    state["scene_state"]["window_evidence"]["captured_at"] += 1
    state["plan_contract"] = {"contract_id": "plan-two", "subgoal": "Try again", "completed_options": []}
    state["recent_actions"] = [{"candidate_id": "click", "verified": False}]
    renewed = replace(original, state=state, subgoal="A freshly worded planner subgoal")
    renewed_preserved = copy.deepcopy(renewed.to_dict())

    assert stable_scene_fingerprint(original) == stable_scene_fingerprint(renewed)
    assert original.to_dict() == preserved
    assert renewed.to_dict() == renewed_preserved
    assert state_fingerprint(original) != state_fingerprint(renewed)


@pytest.mark.parametrize(("path", "value"), [
    (("window_title",), "Done"),
    (("text",), "Completed"),
    (("controls",), [{"name": "Finish", "rectangle": [110, 20, 190, 60]}]),
    (("edit_values",), ["changed"]),
    (("visual_image_hash",), "changed-pixels"),
    (("scene_state", "fingerprint"), "new-scene"),
    (("scene_state", "window_handle"), 43),
    (("scene_state", "captured_at"), "changed scene data"),
    (("scene_state", "window_evidence", "handle"), 43),
    (("scene_state", "window_evidence", "process_id"), 456),
    (("scene_state", "window_evidence", "region"), [20, 0, 300, 300]),
    (("scene_state", "window_evidence", "dpi"), 144),
    (("scene_state", "window_evidence", "foreground_checked"), False),
    (("captured_at",), "app-reported timestamp"),
    (("observed_at",), "app-reported timestamp"),
    (("scene_state", "window_evidence", "observed_at"), "app-specific evidence"),
    (("scene_state", "window_evidence", "application", "captured_at"), 789),
    (("application", "observed_at"), 789),
    (("application", "captured_at"), 789),
    (("application", "plan_contract"), "changed user data"),
])
def test_stable_scene_preserves_physical_changes_and_unrelated_application_data(path, value):
    original = desktop_observation()
    state = copy.deepcopy(original.state)
    target = state
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert stable_scene_fingerprint(original) != stable_scene_fingerprint(replace(original, state=state))


@pytest.mark.parametrize("changed", [{"task": "A new task"}, {"source": "a different observer"}])
def test_stable_scene_keeps_task_and_observer_scope(changed):
    original = desktop_observation()
    assert stable_scene_fingerprint(original) != stable_scene_fingerprint(replace(original, **changed))


def test_renewed_stable_scene_does_not_rebind_old_jev_decision_or_bypass_freshness():
    recent = desktop_observation()
    old_state = copy.deepcopy(recent.state)
    old_state["scene_state"]["observed_at"] -= 60
    old_state["scene_state"]["window_evidence"]["captured_at"] -= 60
    expired = replace(recent, state=old_state)
    candidate = ActionCandidate("click", Channel.GUI, "desktop.click", "Synthetic click")
    decision = Decision(recent.observation_id, "click", {"click": 1}, 1, "simulated-local", 0)

    assert stable_scene_fingerprint(recent) == stable_scene_fingerprint(expired)
    assert JevDecision.bind(decision, recent, [candidate]).scene_fingerprint != (
        JevDecision.bind(decision, expired, [candidate]).scene_fingerprint
    )
    assert old_state["scene_state"]["observed_at"] < time.time() - 30
    with pytest.raises(ObservationExpired, match="expired"):
        ActionGuard().check_fresh(expired, [candidate])

    old_lease = WindowLease(42, 123, (0, 0, 300, 300), 96,
                            captured_at=old_state["scene_state"]["window_evidence"]["captured_at"])
    with pytest.raises(GuardRejected, match="expired"):
        old_lease.validate(WindowLease(42, 123, (0, 0, 300, 300), 96))
