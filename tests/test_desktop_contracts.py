"""Deterministic desktop fixtures, not real Windows or model verification."""

import json

import pytest
from test_open_desktop import FakeDesktopPlanner, FakeWindow

from cua_jev.desktop_contracts import PlanContract
from cua_jev.open_desktop import DesktopPlan, OpenDesktopTask


def task(window, planner=None, **kwargs):
    item = OpenDesktopTask(
        goal="Finish the window",
        window_title_re="Example",
        window=window,
        planner=planner or FakeDesktopPlanner(),
        allow_text_input=True,
        allow_button_actions=True,
        **kwargs,
    )
    item.reset()
    return item


def test_plan_reuses_semantics_across_fill_and_reordered_layout(tmp_path):
    window = FakeWindow()
    planner = FakeDesktopPlanner(
        [{"ref": "c0", "operation": "fill", "value": "Ada"}, {"ref": "c1", "operation": "click"}]
    )
    item = task(window, planner, contract_path=tmp_path / "contract.json")
    observed = item.observe(())
    choices = item.candidates(observed, ())
    first = next(candidate for candidate in choices if candidate.arguments["operation"] == "fill")
    receipt = item(first, observed.observation_id, "fill")
    checked = item._verify_effect(first, receipt)
    assert checked.passed
    item.evaluate(observed, first, receipt, checked)
    # New layout has different refs/index/geometry but identical semantic identity.
    window.controls.reverse()
    for control in window.controls:
        control.index += 10
    observed = item.observe(())
    choices = item.candidates(observed, ())
    assert planner.calls == 1
    assert all(candidate.arguments["name"] == "Finish" for candidate in choices)
    assert choices[0].arguments["ref"] == "c0"
    assert choices[0].arguments["rectangle"][1] > 300
    assert json.loads((tmp_path / "contract.json").read_text())["used"] == [0]
    restored = task(window, contract_path=tmp_path / "contract.json")
    restored.restore_contract()
    assert restored._snapshot is None
    restored.candidates(restored.observe(()), ())
    assert restored.planner_calls == 0
    assert restored.plan_contract.contract_id == item.plan_contract.contract_id
    assert "rectangle" not in json.dumps(item.plan_contract.to_dict())


def test_click_unrelated_change_and_preexisting_expected_do_not_verify():
    window = FakeWindow()
    item = task(window)
    observed = item.observe(())
    candidate = item.candidates(observed, ())[0]
    window.controls[1].invoke = lambda: setattr(window.controls[0], "value", "irrelevant")
    receipt = item(candidate, observed.observation_id, "unrelated")
    assert receipt.success
    assert not item._verify_effect(candidate, receipt).passed
    window.title = "Done"
    item = task(window)
    item.observe(())
    item._call_planner(())
    candidate = item._build_candidates()[0]
    receipt = item(candidate, "obs", "preexisting")
    assert not item._verify_effect(candidate, receipt).passed


def test_ambiguous_semantics_request_replan_and_never_bind_arbitrarily():
    window = FakeWindow()
    item = task(window)
    item.candidates(item.observe(()), ())
    duplicate = window.controls[1]
    window.controls.append(duplicate)
    choices = item.candidates(item.observe(()), ())
    assert choices == ()
    assert item.planner_calls == 2


def test_navigation_requires_bounded_key_and_declared_effect():
    window = FakeWindow()
    item = task(window, allow_navigation_input=True)
    snapshot = item.observe_state(())
    base = {"subgoal": "Navigate", "success": {"kind": "window_title_contains", "value": "Done"}}
    for operation, value in [("press", "enter"), ("scroll", "20"), ("drag", "")]:
        with pytest.raises(ValueError):
            DesktopPlan.from_dict(
                {
                    **base,
                    "options": [
                        {
                            "ref": "c1",
                            "operation": operation,
                            "value": value,
                            "expected": {"kind": "text_contains", "value": "More"},
                        }
                    ],
                },
                snapshot,
            )
    with pytest.raises(ValueError, match="expected"):
        DesktopPlan.from_dict(
            {**base, "options": [{"ref": "c0", "operation": "press", "value": "tab"}]}, snapshot
        )


def test_persisted_contract_rejects_invalid_schema():
    for data in [{}, {"window_handle": 42, "subgoal": "x", "options": [], "contract_id": "x"}]:
        with pytest.raises(ValueError):
            PlanContract.from_dict(data)


def test_restore_does_not_bypass_navigation_and_condition_validation():
    item = task(FakeWindow())
    item.candidates(item.observe(()), ())
    valid = json.loads(json.dumps(item.plan_contract.to_dict()))
    for operation, value in [("press", "win"), ("scroll", "1000000")]:
        payload = json.loads(json.dumps(valid))
        payload["options"][0].update(operation=operation, value=value)
        with pytest.raises(ValueError, match="navigation"):
            PlanContract.from_dict(payload)
    for condition in [{}, {"kind": "always", "value": "x"}, {"kind": "text_contains", "value": ""}]:
        payload = json.loads(json.dumps(valid))
        payload["success"] = condition
        with pytest.raises(ValueError):
            PlanContract.from_dict(payload)


def test_observation_does_not_expose_plan_input_values():
    item = task(
        FakeWindow(),
        FakeDesktopPlanner([{"ref": "c0", "operation": "fill", "value": "private-value-example"}]),
    )
    item.candidates(item.observe(()), ())
    assert "private-value-example" not in json.dumps(item.observe(()).state)


def test_reset_resumes_durable_progress_but_requires_fresh_scene(tmp_path):
    path = tmp_path / "plan.json"
    window = FakeWindow()
    original = task(window, contract_path=path)
    original.candidates(original.observe(()), ())
    resumed = task(window, contract_path=path, resume_contract=True)
    assert resumed._snapshot is None
    with pytest.raises(RuntimeError, match="observe"):
        resumed.candidates(None, ())
    resumed.candidates(resumed.observe(()), ())
    assert resumed.planner_calls == 0
    assert resumed.plan_contract.contract_id == original.plan_contract.contract_id
    with pytest.raises(ValueError, match="contract_path"):
        task(window, resume_contract=True)


def test_navigation_focuses_without_clicking_and_verifies_declared_effect():
    window = FakeWindow()

    class NavigationScreen:
        def press(self, key):
            assert key == "tab"
            window.title = "Navigated"

        def click_point(self, *_args):
            raise AssertionError("navigation must not click its target")

    focused = []
    window.controls[0].set_focus = lambda: focused.append(True)
    item = task(
        window,
        FakeDesktopPlanner(
            [
                {
                    "ref": "c0",
                    "operation": "press",
                    "value": "tab",
                    "expected": {"kind": "window_title_contains", "value": "Navigated"},
                }
            ]
        ),
        screen=NavigationScreen(),
        allow_navigation_input=True,
    )
    observation = item.observe(())
    candidate = item.candidates(observation, ())[0]
    receipt = item(candidate, observation.observation_id, "navigation")
    assert receipt.success
    assert focused == [True]
    assert item._verify_effect(candidate, receipt).passed


def test_fill_verifier_rebinds_semantic_field_after_ref_reorder():
    window = FakeWindow()
    item = task(window, FakeDesktopPlanner([{"ref": "c0", "operation": "fill", "value": "Ada"}]))
    observation = item.observe(())
    candidate = item.candidates(observation, ())[0]
    receipt = item(candidate, observation.observation_id, "fill-reorder")
    window.controls.reverse()
    assert item._verify_effect(candidate, receipt).passed
    window.controls[1].value = "wrong"
    window.controls[0].value = "Ada"
    assert not item._verify_effect(candidate, receipt).passed
