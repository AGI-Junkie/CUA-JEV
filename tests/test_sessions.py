"""Real Chromium integration; MockTransport replaces only model HTTP responses."""

import importlib.util
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from cua_jev.ui.configuration import ConfigurationStore
from cua_jev.ui.sessions import ControlledSessionManager

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not shutil.which("chromium") or importlib.util.find_spec("playwright") is None,
    reason="requires local Chromium and Playwright; no browser downloads in tests",
)


class TestSessionManager(ControlledSessionManager):
    __test__ = False


def wait(manager, sid, status, timeout=12):
    statuses = {status} if isinstance(status, str) else set(status)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = manager.get(sid)
        if state["status"] in statuses and not state.get("command_pending"):
            return state
        assert state["status"] != "failed", state
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {statuses}: {manager.get(sid)}")


@pytest.fixture
def manager(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    manager = TestSessionManager(ROOT, tmp_path / "sessions", store)
    yield manager
    manager.close()


def create(manager, **payload):
    state = manager.create(
        {
            "task": "Save the requested local note",
            "context": "user context used",
            "note_text": "Distinct user note 中文",
            **payload,
        }
    )
    return wait(manager, state["id"], "scope_ready")["id"]


def start(manager, sid):
    manager.command(sid, "confirm_scope")
    wait(manager, sid, "ready")
    manager.command(sid, "start")


@pytest.mark.parametrize("layout", ["columns", "stacked"])
def test_real_browser_persistent_plan_and_exact_note(manager, layout):
    sid = create(manager, layout=layout)
    assert manager.frame_path(sid).read_bytes().startswith(b"\x89PNG")
    start(manager, sid)
    state = wait(manager, sid, "completed")
    events = manager.events(sid)["events"]
    kinds = [e["type"] for e in events]
    assert all(
        k in kinds for k in ("plan", "jev_decision", "decision", "receipt", "verification", "completion")
    )
    assert len([e for e in events if e["type"] == "receipt"]) == 2
    assert all(e["data"]["passed"] for e in events if e["type"] == "verification")
    assert state["counters"]["planner"]["calls"] == 1
    assert state["counters"]["jev"]["calls"] == 2
    assert state["counters"]["steps"] == 2
    assert state["counters"]["model_attempts"] == 3
    request = next(e["data"] for e in events if e["type"] == "model_request")
    assert "user context used" in request["goal"] and "Distinct user note 中文" in request["goal"]
    assert events[-2]["data"]["actual"] == "Saved locally: Distinct user note 中文"
    assert manager.frame_path(sid).read_bytes().startswith(b"\x89PNG")
    contract = json.loads((manager.directory / sid / "plan.json").read_text())
    assert contract["contract"]["contract_id"] == state["plan"]["contract_id"]
    assert "rectangle" not in json.dumps(contract["contract"])
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
    assert manager.events(sid, after=events[-1]["seq"])["events"] == []


def test_duplicate_start_and_session_switch_require_stop_ack(manager):
    sid = create(manager, _test_faults={"planner_delay": 0.3})
    start(manager, sid)
    with pytest.raises(ValueError):
        manager.command(sid, "start")
    with pytest.raises(ValueError, match="active session"):
        manager.create({"task": "switch", "note_text": "other"})
    manager.command(sid, "stop")
    assert manager.get(sid)["status"] == "stopping"
    wait(manager, sid, "stopped")
    sid2 = create(manager)
    assert sid != sid2


def test_pause_late_planner_then_correct_reconfirm_and_resume(manager):
    sid = create(manager, _test_faults={"planner_delay": 0.4})
    start(manager, sid)
    wait(manager, sid, "running")
    time.sleep(0.15)
    manager.command(sid, "pause")
    assert manager.get(sid)["status"] == "pausing"
    wait(manager, sid, "paused")
    assert not [e for e in manager.events(sid)["events"] if e["type"] == "receipt"]
    manager.command(sid, "takeover")
    wait(manager, sid, "takeover")
    manager.command(sid, "correct", {"note_text": "Corrected note", "layout": "stacked"})
    end = time.monotonic() + 5
    while manager.get(sid).get("command_pending") and time.monotonic() < end:
        time.sleep(0.02)
    assert not manager.get(sid)["scope_confirmed"]
    with pytest.raises(ValueError, match="Confirm"):
        manager.command(sid, "resume")
    manager.command(sid, "confirm_scope")
    wait(manager, sid, "ready")
    manager.command(sid, "resume")
    wait(manager, sid, "completed")
    completion = next(e for e in manager.events(sid)["events"] if e["type"] == "completion")
    assert completion["data"]["actual"] == "Saved locally: Corrected note"


def test_stop_late_jev_never_dispatches(manager):
    sid = create(manager, _test_faults={"jev_delay": 0.5})
    start(manager, sid)
    wait(manager, sid, "running")
    end = time.monotonic() + 6
    while manager._sessions[sid].ledger.public()["attempts_by_role"].get("jev", 0) < 1:
        assert time.monotonic() < end
        time.sleep(0.01)
    manager.command(sid, "stop")
    stopped = wait(manager, sid, "stopped")
    assert stopped["counters"]["model_attempts"] == 2
    assert stopped["counters"]["attempts_by_role"] == {"planner": 1, "jev": 1}
    assert stopped["counters"]["jev"]["calls"] == 1
    assert stopped["counters"]["steps"] == 0
    assert "model_timing_complete" in stopped["counters"]
    time.sleep(0.1)
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])


def test_step_budget_blocks_second_input_and_survives_resume(manager):
    manager.configuration.update({"limits": {"max_steps": 1}})
    sid = create(manager)
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert "Step budget" in state["error"]
    assert len([e for e in manager.events(sid)["events"] if e["type"] == "receipt"]) == 1
    manager.command(sid, "resume")
    wait(manager, sid, "paused")
    assert len([e for e in manager.events(sid)["events"] if e["type"] == "receipt"]) == 1


def test_model_attempt_budget_blocks_jev(manager):
    manager.configuration.update({"limits": {"max_model_attempts": 1}})
    sid = create(manager)
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert "attempt budget" in state["error"]
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])


def test_timeout_error_recovery_does_not_invent_input(manager):
    sid = create(manager, _test_faults={"planner_timeout": True})
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert "request failed" in state["error"]
    manager.command(sid, "reobserve")
    wait(manager, sid, "paused")
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])
    manager._sessions[sid].faults.clear()
    manager.command(sid, "replan")
    wait(manager, sid, "paused")
    manager.command(sid, "resume")
    wait(manager, sid, "completed")


def test_history_restart_marks_interrupted(manager):
    sid = create(manager)
    recovered = ControlledSessionManager(ROOT, manager.directory)
    assert recovered.get(sid)["status"] == "interrupted"
    assert recovered.events(sid)["events"]
    with pytest.raises(ValueError, match="Historical"):
        recovered.command(sid, "resume")


def test_scope_and_external_api_fail_closed(manager):
    with pytest.raises(ValueError, match="disabled"):
        manager.create({"task": "x", "note_text": "x", "mode": "live"})
    sid = create(manager)
    with pytest.raises(ValueError):
        manager.command(sid, "start")
    manager.command(sid, "confirm_scope")
    wait(manager, sid, "ready")
    manager.command(sid, "invalidate_scope")
    wait(manager, sid, "scope_ready")
    assert manager.get(sid)["scope_confirmed"] is False


def test_immediate_correct_resume_cannot_bypass_scope(manager):
    sid = create(manager, _test_faults={"planner_delay": 0.4})
    start(manager, sid)
    wait(manager, sid, "running")
    manager.command(sid, "pause")
    wait(manager, sid, "paused")
    assert manager.get(sid)["scope_confirmed"]
    manager.command(sid, "correct", {"note_text": "changed"})
    with pytest.raises(ValueError):
        manager.command(sid, "resume")
    wait(manager, sid, "takeover")
    assert manager.get(sid)["scope_confirmed"] is False
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])


def test_zero_retry_budget_rejects_planner_retry(manager):
    manager.configuration.update({"limits": {"max_retries": 0}})
    sid = create(manager, _test_faults={"planner_timeout": True})
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert "retry budget" in state["error"]
    assert state["counters"]["model_attempts"] == 1


def test_no_effect_click_is_not_completion(manager):
    sid = create(manager, _test_faults={"no_effect": True})
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert "independently verified" in state["error"]
    verifications = [e for e in manager.events(sid)["events"] if e["type"] == "verification"]
    assert len(verifications) == 2
    assert verifications[0]["data"]["passed"]
    assert not verifications[1]["data"]["passed"]
    assert not any(e["type"] == "completion" for e in manager.events(sid)["events"])


def test_lost_focus_blocks_input(manager):
    sid = create(manager, _test_faults={"lost_focus": True})
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert state["error"]
    receipts = [e["data"] for e in manager.events(sid)["events"] if e["type"] == "receipt"]
    assert receipts and not receipts[0]["success"]
    assert not any(e["type"] == "completion" for e in manager.events(sid)["events"])


def test_stop_during_queued_start_acknowledges_stopped(manager):
    sid = create(manager)
    manager.command(sid, "confirm_scope")
    wait(manager, sid, "ready")
    manager.command(sid, "start")
    manager.command(sid, "stop")
    wait(manager, sid, "stopped")
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])


def test_policy_timeout_then_correction_preserves_outstanding_gate(manager):
    manager.configuration.update({"limits": {"decision_timeout_seconds": 0.1}})
    sid = create(manager, _test_faults={"jev_delay": 0.5})
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert "deadline" in state["error"]
    manager.command(sid, "correct", {"note_text": "corrected after timeout"})
    wait(manager, sid, "takeover")
    manager.command(sid, "confirm_scope")
    wait(manager, sid, "ready")
    manager.command(sid, "resume")
    state = wait(manager, sid, "paused")
    # At most one request can be in flight; timing may allow it to finish before resume.
    assert "outstanding" in state["error"] or "deadline" in state["error"]
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])


def test_configured_model_roles_reach_mock_adapters_without_key_leak(manager):
    manager.configuration.update(
        {
            "roles": {
                "planner": {
                    "name": "my-planner-model",
                    "endpoint": "https://planner.example/custom",
                    "api_key": "PRIVATE_TEST_PLANNER_KEY",
                },
                "jev": {
                    "name": "my-jev-model",
                    "endpoint": "https://jev.example/custom",
                    "api_key": "PRIVATE_TEST_JEV_KEY",
                },
            }
        }
    )
    sid = create(manager)
    start(manager, sid)
    wait(manager, sid, "completed")
    events = manager.events(sid)["events"]
    requests = [e["data"] for e in events if e["type"] == "model_request"]
    assert [r["model"] for r in requests] == ["my-planner-model", "my-jev-model", "my-jev-model"]
    persisted = "".join(p.read_text() for p in (manager.directory / sid).glob("*.json*"))
    assert "PRIVATE_TEST" not in persisted
    assert "PRIVATE_TEST" not in json.dumps(manager.get(sid))


def test_wall_deadline_blocks_late_model_result(manager):
    manager.configuration.update({"limits": {"max_wall_seconds": 1}})
    sid = create(manager, _test_faults={"jev_delay": 1.1})
    start(manager, sid)
    state = wait(manager, sid, "paused")
    assert "wall time" in state["error"]
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])


def test_arbitrary_target_overrides_are_rejected(manager):
    with pytest.raises(ValueError, match="Unsupported"):
        manager.create({"task": "x", "note_text": "x", "url": "https://external.example"})
    sid = create(manager)
    with pytest.raises(ValueError, match="Only correct"):
        manager.command(sid, "confirm_scope", {"url": "https://external.example"})


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
def test_session_private_directory(manager):
    sid = create(manager)
    assert (manager.directory / sid).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("layout", ["columns", "stacked"])
def test_real_screenshot_mock_vision_selects_and_verifies_visual_click(manager, layout):
    sid = create(manager, layout=layout, observe_mode="mock_vision")
    assert not [e for e in manager.events(sid)["events"] if e["type"] == "vision_mock"]
    assert manager.get(sid)["counters"] == {}  # Preview never calls models before scope consent.
    start(manager, sid)
    state = wait(manager, sid, "completed")
    events = manager.events(sid)["events"]
    vision_events = [e for e in events if e["type"] == "vision_mock"]
    assert vision_events and all(e["data"]["pixel_sha256"] for e in vision_events)
    receipts = [e["data"] for e in events if e["type"] == "receipt"]
    assert receipts[-1]["capability"] == "desktop.visual_click"
    assert all(e["data"]["passed"] for e in events if e["type"] == "verification")
    assert state["counters"]["attempts_by_role"]["vision"] == len(vision_events)
    assert state["counters"]["vision"]["calls"] == len(vision_events)
    assert state["counters"]["planner"]["calls"] == 1
    assert state["counters"]["jev"]["calls"] == 2
    assert state["observe_mode"] == "mock_vision"
    assert "simulated" in state["capabilities"]["observer"]
    trace = (manager.directory / sid / "events.jsonl").read_text()
    assert "data:image" not in trace


def test_vision_budget_refusal_is_visible_and_dom_verifies_completed_effect(manager):
    manager.configuration.update({"limits": {"max_model_attempts": 7}})
    sid = create(manager, observe_mode="mock_vision")
    start(manager, sid)
    state = wait(manager, sid, "completed")
    counters = state["counters"]
    assert counters["model_attempts"] == 7
    assert counters["vision"]["failures"] == 1
    assert counters["vision"]["calls"] == counters["attempts_by_role"]["vision"] == 4
    assert counters["vision"]["attempts"] == 5  # Last invocation refused before transport work.
    events = manager.events(sid)["events"]
    assert len([e for e in events if e["type"] == "receipt"]) == 2
    completion = next(e for e in events if e["type"] == "completion")
    assert completion["data"]["observer"] == "live DOM"
    assert completion["data"]["actual"] == "Saved locally: Distinct user note 中文"


@pytest.mark.parametrize("fault", ["planner_json", "planner_ref", "jev_choice"])
def test_invalid_model_reply_fails_closed_then_recovers(manager, monkeypatch, fault):
    """Corrupt one real adapter response, retaining its counted fixture transport attempt."""
    import httpx

    original_transport = httpx.MockTransport
    remaining = [True]

    def transport(handler):
        def corrupt(request):
            response = handler(request)
            role = "planner" if request.url.path.endswith("chat/completions") else "jev"
            expected_role = "jev" if fault == "jev_choice" else "planner"
            if remaining[0] and role == expected_role:
                remaining[0] = False
                data = response.json()
                if fault == "planner_json":
                    data["choices"][0]["message"]["content"] = "not valid JSON"
                elif fault == "planner_ref":
                    plan = json.loads(data["choices"][0]["message"]["content"])
                    plan["options"][0]["ref"] = "unobserved-target"
                    data["choices"][0]["message"]["content"] = json.dumps(plan)
                else:
                    data["answers"]["action"]["choice"] = "unoffered-action"
                return httpx.Response(200, json=data)
            return response

        return original_transport(corrupt)

    monkeypatch.setattr(httpx, "MockTransport", transport)
    sid = create(manager)
    start(manager, sid)
    failed = wait(manager, sid, "paused")
    assert failed["error"]
    assert not remaining[0]
    facts = manager.events(sid)["events"]
    assert not any(e["type"] in {"receipt", "completion"} for e in facts)
    attempts_at_error = failed["counters"]["model_attempts"]
    assert attempts_at_error == (2 if fault == "jev_choice" else 1)
    manager.command(sid, "replan")
    wait(manager, sid, "paused")
    manager.command(sid, "resume")
    completed = wait(manager, sid, "completed")
    assert completed["counters"]["model_attempts"] == attempts_at_error + 3
    assert completed["counters"]["steps"] == 2
    receipts = [e for e in manager.events(sid)["events"] if e["type"] == "receipt"]
    assert len(receipts) == 2 and all(e["data"]["success"] for e in receipts)


def test_stale_scene_guard_reobserves_without_replanning(manager, monkeypatch):
    from dataclasses import replace

    from cua_jev.runtime import AgentRuntime

    original_step = AgentRuntime.step
    corrupt_once = [True]

    def stale_once(runtime, observation, candidates):
        if corrupt_once[0]:
            corrupt_once[0] = False
            observation = replace(observation, created_at=time.time() - 60)
        return original_step(runtime, observation, candidates)

    monkeypatch.setattr(AgentRuntime, "step", stale_once)
    sid = create(manager)
    start(manager, sid)
    paused = wait(manager, sid, "paused")
    assert "expired" in paused["error"]
    contract_id = paused["plan"]["contract_id"]
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])
    manager.command(sid, "reobserve")
    wait(manager, sid, "paused")
    manager.command(sid, "resume")
    completed = wait(manager, sid, "completed")
    assert completed["plan"]["contract_id"] == contract_id
    assert completed["counters"]["planner"]["calls"] == 1
    assert completed["counters"]["jev"]["calls"] == 3
    assert completed["counters"]["steps"] == 2


def test_no_fresh_candidates_requests_recovery_without_blind_input(manager, monkeypatch):
    from cua_jev.desktop_contracts import FreshCandidateCompiler

    original_bind = FreshCandidateCompiler.bind
    unavailable = [True]

    def unavailable_bind(compiler, contract, snapshot, used):
        return {} if unavailable[0] else original_bind(compiler, contract, snapshot, used)

    monkeypatch.setattr(FreshCandidateCompiler, "bind", unavailable_bind)
    sid = create(manager)
    start(manager, sid)
    paused = wait(manager, sid, "paused")
    assert "No freshly grounded candidates" in paused["error"]
    assert paused["counters"]["jev"]["calls"] == 0
    assert not any(e["type"] == "receipt" for e in manager.events(sid)["events"])
    unavailable[0] = False
    manager.command(sid, "replan")
    wait(manager, sid, "paused")
    manager.command(sid, "resume")
    completed = wait(manager, sid, "completed")
    assert completed["counters"]["jev"]["calls"] == 2
    assert completed["counters"]["steps"] == 2
