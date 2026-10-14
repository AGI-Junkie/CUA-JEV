"""Review regressions with deterministic fake UIA, no Windows/API invocation."""
import json

import pytest
from test_desktop_contracts import SequentialPlanner, task
from test_open_desktop import FakeWindow

from cua_jev.open_desktop import DesktopPlan


@pytest.mark.parametrize("used", [[1], [0, 0], [1, 0]])
def test_resume_rejects_nonprefix_duplicated_or_reordered_progress(tmp_path, used):
    item = task(FakeWindow(), SequentialPlanner([
        {"ref": "c0", "operation": "fill", "value": "Ada"},
        {"ref": "c1", "operation": "click"},
    ]), contract_path=tmp_path / "plan.json")
    item.candidates(item.observe(()), ())
    payload = json.loads(item.contract_path.read_text())
    payload["used"] = used
    item.contract_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="prefix"):
        item.restore_contract()
    assert item._used == set()


def test_resume_accepts_strict_ordered_unique_prefix(tmp_path):
    item = task(FakeWindow(), SequentialPlanner([
        {"ref": "c0", "operation": "fill", "value": "Ada"},
        {"ref": "c1", "operation": "click"},
    ]), contract_path=tmp_path / "plan.json")
    item.candidates(item.observe(()), ())
    payload = json.loads(item.contract_path.read_text())
    payload["used"] = [0, 1]
    item.contract_path.write_text(json.dumps(payload))
    item.restore_contract()
    assert item._used == {0, 1}


@pytest.mark.parametrize("invalid", [{}, [], 1, None])
@pytest.mark.parametrize("field", ["expected", "success", "operation"])
def test_plan_malformed_kind_and_operation_raise_controlled_value_error(invalid, field):
    item = task(FakeWindow())
    snapshot = item._capture()
    data = {"subgoal": "Finish", "options": [{"ref": "c1", "operation": "click"}],
            "success": {"kind": "window_title_contains", "value": "Done"}}
    if field == "expected":
        data["options"][0]["expected"] = {"kind": invalid, "value": "Done"}
    elif field == "success":
        data["success"]["kind"] = invalid
    else:
        data["options"][0]["operation"] = invalid
    with pytest.raises(ValueError):
        DesktopPlan.from_dict(data, snapshot)
