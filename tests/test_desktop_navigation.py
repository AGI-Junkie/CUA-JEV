"""Synthetic pointer and UIA evidence; no host input or model service is used."""

from types import SimpleNamespace

import pytest
from test_open_desktop import FakeDesktopPlanner, FakeDesktopScreen, FakeWindow

from cua_jev.cli import _open_desktop_runner
from cua_jev.errors import GuardRejected
from cua_jev.executors.screen import ScreenController
from cua_jev.open_desktop import OpenDesktopTask


class NavigationScreen(FakeDesktopScreen):
    guarded_scroll_point = ScreenController.guarded_scroll_point
    pause_s = 0
    move_s = 0

    def __init__(self, window):
        self.window = window
        self.pointer = (-300, -300)
        self.events = []
        self.foreground = True
        self.on_move = lambda: None

    def ensure_foreground(self, handle):
        super().ensure_foreground(handle)
        if not self.foreground:
            raise GuardRejected("selected window is not foreground")

    def _modules(self):
        return self, None

    def moveTo(self, x, y, **_kwargs):
        self.pointer = (x, y)
        self.events.append(("move", x, y))
        self.on_move()

    def position(self):
        return self.pointer

    def scroll(self, ticks):
        self.events.append(("wheel", ticks, *self.pointer))
        self.window.title = "Scrolled"

    def press(self, key):
        self.events.append(("key", key))
        self.window.title = "Scrolled"

    def click(self):
        raise AssertionError("navigation must never click")


def navigation_task(operation="scroll", *, include_button=False):
    window = FakeWindow()
    screen = NavigationScreen(window)
    window.controls[0].set_focus = lambda: screen.events.append(("focus",))
    option = {
        "ref": "c0", "operation": operation,
        "value": "1" if operation == "scroll" else "tab",
        "expected": {"kind": "window_title_contains", "value": "Scrolled"},
    }
    options = [option]
    if include_button:
        options.append({"ref": "c1", "operation": "click"})
    task = OpenDesktopTask(
        goal="Finish the window", window_title_re="Example", window=window,
        screen=screen, planner=FakeDesktopPlanner(options), allow_navigation_input=True,
    )
    task.reset()
    observation = task.observe(())
    candidates = task.candidates(observation, ())
    return task, observation, candidates, screen


def test_scroll_moves_from_another_application_to_its_grounded_control_without_clicking():
    task, observation, candidates, screen = navigation_task()
    receipt = task(candidates[0], observation.observation_id, "scroll")
    assert receipt.success, receipt.error
    assert screen.events == [("focus",), ("move", 55, 22), ("wheel", 1, 55, 22)]
    assert task._verify_effect(candidates[0], receipt).passed


@pytest.mark.parametrize("changed", ["geometry", "ambiguous", "expected", "focus", "pointer"])
def test_scroll_refuses_changes_during_pointer_movement(changed):
    task, observation, candidates, screen = navigation_task()

    def change_target():
        if changed == "geometry":
            task.window.controls[0].index += 10
        elif changed == "ambiguous":
            task.window.controls.append(task.window.controls[0])
        elif changed == "expected":
            task.window.title = "Scrolled"
        elif changed == "focus":
            screen.foreground = False
        else:
            screen.pointer = (-300, -300)

    screen.on_move = change_target
    receipt = task(candidates[0], observation.observation_id, "changed-scroll")
    assert not receipt.success
    assert screen.events == [("focus",), ("move", 55, 22)]


def test_scroll_refuses_missing_physical_window_evidence():
    task, observation, candidates, screen = navigation_task()
    task._window_lease = None
    receipt = task(candidates[0], observation.observation_id, "unbound-scroll")
    assert not receipt.success
    assert "physical window evidence" in receipt.error
    assert screen.events == []


@pytest.mark.parametrize("operation", ["press", "scroll"])
def test_cli_navigation_flag_authorizes_navigation_without_offering_button_actions(operation):
    task, observation, candidates, screen = navigation_task(operation, include_button=True)
    assert len(candidates) == 1
    assert candidates[0].arguments["operation"] == operation
    args = SimpleNamespace(
        policy="rule", allow_text_input=False, allow_button_actions=False,
        allow_navigation_input=True, trace=None, max_steps=1,
    )
    runtime = _open_desktop_runner(args).runtime_factory(task)
    step = runtime.step(observation, candidates)
    assert step.receipt.success, step.receipt.error
    assert step.verification.passed
    if operation == "press":
        assert screen.events == [("focus",), ("key", "tab")]
