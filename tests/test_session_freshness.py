"""Real Chromium input with synthetic expired frames and mock model HTTP."""

import importlib.util
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

from cua_jev.open_desktop import OpenDesktopTask
from cua_jev.ui.configuration import ConfigurationStore
from cua_jev.ui.sessions import ControlledSessionManager

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not shutil.which("chromium") or importlib.util.find_spec("playwright") is None,
    reason="requires local Chromium and Playwright; no browser downloads in tests",
)


def wait(manager, sid, status):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        state = manager.get(sid)
        if state["status"] == status and not state.get("command_pending"):
            return state
        assert state["status"] != "failed", state
        time.sleep(0.02)
    pytest.fail(f"Timed out waiting for {status}: {manager.get(sid)}")


@pytest.fixture
def manager(tmp_path):
    configuration = ConfigurationStore(tmp_path / "config.json")
    manager = ControlledSessionManager(ROOT, tmp_path / "sessions", configuration)
    yield manager
    manager.close()


def start(manager):
    sid = manager.create({"task": "Save synthetic local note", "note_text": "Fresh input"})["id"]
    wait(manager, sid, "scope_ready")
    manager.command(sid, "confirm_scope")
    wait(manager, sid, "ready")
    manager.command(sid, "start")
    return sid


def stale_observer(monkeypatch, should_age):
    observe = OpenDesktopTask.observe

    def aged(self, history, **kwargs):
        observation = observe(self, history, **kwargs)
        if should_age(self, history):
            observation = replace(observation, created_at=time.time() - 31)
        return observation

    monkeypatch.setattr(OpenDesktopTask, "observe", aged)


def test_session_refreshes_slow_planner_frame_before_jev_and_verifies_real_input(manager, monkeypatch):
    # The first planned observation ages beyond the guard while its semantic
    # plan remains reusable; the next observation must produce a fresh binding.
    stale_observer(monkeypatch, lambda task, history: not history and task.plan_contract is None)
    sid = start(manager)
    state = wait(manager, sid, "completed")
    events = manager.events(sid)["events"]
    assert state["counters"]["observation_refreshes"] == 1
    assert state["counters"]["planner"]["calls"] == 1
    assert state["counters"]["jev"]["calls"] == 2
    assert state["counters"]["model_attempts"] == 3
    assert state["counters"]["steps"] == 2
    assert len([event for event in events if event["type"] == "receipt"]) == 2
    assert all(event["data"]["passed"] for event in events if event["type"] == "verification")
    refresh = next(event for event in events if event["type"] == "observation_refresh")
    first_decision = next(event for event in events if event["type"] == "decision")
    assert refresh["seq"] < first_decision["seq"]


def test_session_refresh_budget_survives_resume_without_model_or_input(manager, monkeypatch):
    manager.configuration.update({"limits": {"max_retries": 1}})
    stale_observer(monkeypatch, lambda task, history: True)
    sid = start(manager)
    state = wait(manager, sid, "paused")
    assert "refresh budget exhausted" in state["error"]
    assert state["counters"]["observation_refreshes"] == 1
    assert state["counters"]["model_attempts"] == 1
    manager.command(sid, "resume")
    resumed = wait(manager, sid, "paused")
    assert resumed["counters"]["observation_refreshes"] == 1
    assert resumed["counters"]["model_attempts"] == 1
    assert not any(event["type"] == "receipt" for event in manager.events(sid)["events"])


def test_zero_session_retry_budget_refuses_stale_frame_without_jev(manager, monkeypatch):
    manager.configuration.update({"limits": {"max_retries": 0}})
    stale_observer(monkeypatch, lambda task, history: True)
    sid = start(manager)
    state = wait(manager, sid, "paused")
    assert "refresh budget exhausted" in state["error"]
    assert state["counters"]["observation_refreshes"] == 0
    assert state["counters"]["attempts_by_role"] == {"planner": 1}
    assert state["counters"]["steps"] == 0


def test_refresh_does_not_reset_model_attempt_budget(manager, monkeypatch):
    manager.configuration.update({"limits": {"max_model_attempts": 1}})
    stale_observer(monkeypatch, lambda task, history: not history and task.plan_contract is None)
    sid = start(manager)
    state = wait(manager, sid, "paused")
    assert "attempt budget" in state["error"]
    assert state["counters"]["observation_refreshes"] == 1
    assert state["counters"]["model_attempts"] == 1
    assert state["counters"]["steps"] == 0
