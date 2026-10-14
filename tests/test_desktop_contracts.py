"""Deterministic desktop fixtures, not real Windows or model verification."""

import json
import os
import stat
from dataclasses import replace

import pytest
from test_open_desktop import FakeDesktopPlanner, FakeDesktopScreen, FakeWindow

from cua_jev.desktop_contracts import PlanContract, verify_postcondition
from cua_jev.open_desktop import DesktopPlan, OpenDesktopTask


def test_intermediate_sequence_click_requires_effect_before_offer():
    window = FakeWindow()
    item = task(window)
    snapshot = item.observe(())
    with pytest.raises(ValueError, match="intermediate sequential"):
        DesktopPlan.from_dict({
            "subgoal": "Click then fill",
            "mode": "sequential",
            "options": [{"ref": "c1", "operation": "click"},
                        {"ref": "c0", "operation": "fill", "value": "Ada"}],
            "success": {"kind": "window_title_contains", "value": "Done"},
        }, item._snapshot)
    # Trusted adapter objects still pass through the same compiler restriction.
    item.planner = SequentialPlanner([
        {"ref": "c1", "operation": "click"},
        {"ref": "c0", "operation": "fill", "value": "Ada"},
    ])
    assert item.candidates(snapshot, ()) == ()
    assert window.controls[0].value == ""
    assert window.window_text() == "Example"


class SequentialPlanner(FakeDesktopPlanner):
    def plan(self, goal, snapshot, recent_actions):
        return replace(super().plan(goal, snapshot, recent_actions), mode="sequential")


def task(window, planner=None, **kwargs):
    kwargs.setdefault("screen", FakeDesktopScreen())
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
    planner = SequentialPlanner(
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
    assert item._build_candidates() == ()  # Preexisting effects cannot authorize another click.


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
    original = task(window, SequentialPlanner(), contract_path=path)
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits; Windows needs directory ACL validation")
def test_checkpoint_replacement_does_not_inherit_public_file_mode(tmp_path):
    path = tmp_path / "contract.json"
    path.write_text("old public file")
    path.chmod(0o644)
    item = task(FakeWindow(), SequentialPlanner(), contract_path=path)
    item.candidates(item.observe(()), ())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["contract"]["contract_id"] == item.plan_contract.contract_id
    assert list(tmp_path.iterdir()) == [path]


def test_legacy_alternatives_do_not_execute_remaining_options_as_sequence(tmp_path):
    window = FakeWindow()
    planner = FakeDesktopPlanner(
        [
            {"ref": "c0", "operation": "fill", "value": "Ada"},
            {"ref": "c1", "operation": "click"},
        ]
    )
    item = task(window, planner, contract_path=tmp_path / "alternatives.json")
    observation = item.observe(())
    candidates = item.candidates(observation, ())
    assert {candidate.arguments["operation"] for candidate in candidates} == {"fill", "click"}
    fill = next(candidate for candidate in candidates if candidate.arguments["operation"] == "fill")
    receipt = item(fill, observation.observation_id, "alternative")
    verification = item._verify_effect(fill, receipt)
    outcome = item.evaluate(observation, fill, receipt, verification)
    assert outcome.reason == "alternative_verified_replan"
    assert item._plan is None and item.plan_contract is None
    assert not (tmp_path / "alternatives.json").exists()
    item.candidates(item.observe(()), ())
    assert planner.calls == 2


def test_explicit_sequence_exposes_only_next_action_and_persists_mode(tmp_path):
    window = FakeWindow()
    planner = SequentialPlanner(
        [
            {"ref": "c0", "operation": "fill", "value": "Ada"},
            {"ref": "c1", "operation": "click"},
        ]
    )
    item = task(window, planner, contract_path=tmp_path / "sequence.json")
    candidates = item.candidates(item.observe(()), ())
    assert {candidate.arguments["operation"] for candidate in candidates} == {"fill"}
    assert json.loads((tmp_path / "sequence.json").read_text())["contract"]["mode"] == "sequential"


def test_desktop_parser_rejects_extra_postcondition_fields_and_invalid_mode():
    item = task(FakeWindow())
    snapshot = item.observe_state(())
    raw = {
        "subgoal": "Save",
        "options": [{"ref": "c1", "operation": "click"}],
        "success": {"kind": "window_title_contains", "value": "Done"},
    }
    with pytest.raises(ValueError, match="mode"):
        DesktopPlan.from_dict({**raw, "mode": "ordered-ish"}, snapshot)
    with pytest.raises(ValueError, match="expected"):
        DesktopPlan.from_dict(
            {
                **raw,
                "options": [
                    {
                        "ref": "c1",
                        "operation": "click",
                        "expected": {"kind": "text_contains", "value": "Saved", "extra": "ignored"},
                    }
                ],
            },
            snapshot,
        )
    with pytest.raises(ValueError, match="success"):
        DesktopPlan.from_dict({**raw, "success": {**raw["success"], "extra": "ignored"}}, snapshot)


def test_resume_rejects_legacy_ambiguous_contract_without_mode(tmp_path):
    item = task(FakeWindow(), SequentialPlanner(), contract_path=tmp_path / "plan.json")
    item.candidates(item.observe(()), ())
    payload = json.loads((tmp_path / "plan.json").read_text())
    del payload["contract"]["mode"]
    (tmp_path / "plan.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="contract"):
        item.restore_contract()


def test_alternatives_replan_invalidates_older_sequential_checkpoint(tmp_path):
    path = tmp_path / "plan.json"
    item = task(FakeWindow(), SequentialPlanner(), contract_path=path)
    item.candidates(item.observe(()), ())
    assert path.exists()
    item.planner = FakeDesktopPlanner()
    item._plan = None
    item.candidates(item.observe(()), ())
    assert item.plan_contract.mode == "alternatives"
    assert not path.exists()


def test_visual_text_cannot_independently_verify_expected_effect():
    item = task(FakeWindow())
    snapshot = replace(item.observe_state(()), visual_text=("Saved successfully",))
    assert not verify_postcondition({"kind": "text_contains", "value": "Saved successfully"}, snapshot)


def completed_fill_task(window, path):
    planner = SequentialPlanner(
        [
            {"ref": "c0", "operation": "fill", "value": "Ada"},
            {"ref": "c1", "operation": "click"},
        ]
    )
    item = task(window, planner, contract_path=path)
    observation = item.observe(())
    candidate = item.candidates(observation, ())[0]
    receipt = item(candidate, observation.observation_id, "completed-fill")
    verification = item._verify_effect(candidate, receipt)
    assert verification.passed
    item.evaluate(observation, candidate, receipt, verification)
    return item, planner


@pytest.mark.parametrize("resume", [False, True])
def test_changed_completed_fill_requires_replan_before_save(tmp_path, resume):
    window = FakeWindow()
    item, planner = completed_fill_task(window, tmp_path / "plan.json")
    window.controls[0].value = ""
    if resume:
        planner = SequentialPlanner(
            [
                {"ref": "c0", "operation": "fill", "value": "Ada"},
                {"ref": "c1", "operation": "click"},
            ]
        )
        item = task(window, planner, contract_path=tmp_path / "plan.json", resume_contract=True)
    calls_before = planner.calls
    candidates = item.candidates(item.observe(()), ())
    assert planner.calls == calls_before + 1
    assert {candidate.arguments["operation"] for candidate in candidates} == {"fill"}
    assert item._used == set()


def test_unchanged_resumed_fill_reuses_plan_after_ref_reordering(tmp_path):
    window = FakeWindow()
    original, _ = completed_fill_task(window, tmp_path / "plan.json")
    window.controls.reverse()
    resumed = task(window, contract_path=tmp_path / "plan.json", resume_contract=True)
    candidates = resumed.candidates(resumed.observe(()), ())
    assert resumed.planner_calls == 0
    assert resumed.plan_contract.contract_id == original.plan_contract.contract_id
    assert {candidate.arguments["name"] for candidate in candidates} == {"Finish"}


def test_ambiguous_completed_field_rejects_old_next_action(tmp_path):
    window = FakeWindow()
    item, _ = completed_fill_task(window, tmp_path / "plan.json")
    window.controls.append(window.controls[0])
    item.observe(())
    assert item._build_candidates() == ()


def test_completed_nonfill_requires_current_independent_effect(tmp_path):
    from cua_jev.open_desktop import DesktopOption

    window = FakeWindow()
    item = task(window)
    item.observe(())
    item._plan = DesktopPlan(
        "Continue",
        (
            DesktopOption("c1", "click", expected={"kind": "window_title_contains", "value": "Saved"}),
            DesktopOption("c0", "fill", "Ada"),
        ),
        "window_title_contains",
        "Done",
        "sequential",
    )
    item.plan_contract = PlanContract.ground(item._plan, item._snapshot)
    item._used = {0}
    assert not item._completed_prerequisites_hold()
    window.title = "Saved"
    item.observe(())
    assert item._completed_prerequisites_hold()
    assert {candidate.arguments["operation"] for candidate in item._build_candidates()} == {"fill"}


def test_completed_fill_changed_during_model_choice_blocks_dispatch(tmp_path):
    window = FakeWindow()
    item, _ = completed_fill_task(window, tmp_path / "plan.json")
    observation = item.observe(())
    candidate = item.candidates(observation, ())[0]
    window.controls[0].value = ""
    receipt = item(candidate, observation.observation_id, "stale-prerequisite")
    assert not receipt.success
    assert "prerequisite changed" in receipt.error
    assert window.title == "Example"
