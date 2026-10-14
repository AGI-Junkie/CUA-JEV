"""Real browser acceptance, with explicitly simulated model API responses."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from cua_jev.controlled_browser import BrowserScreen


@pytest.mark.skipif(not shutil.which("chromium") or importlib.util.find_spec("playwright") is None,
                    reason="requires local Chromium and playwright; no downloads during tests")
def test_two_layouts_real_input_reuses_plan(tmp_path):
    module_path = Path(__file__).parents[1] / "scripts/run_controlled_acceptance.py"
    spec = importlib.util.spec_from_file_location("controlled_acceptance", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = module.run(tmp_path)
    assert len(evidence["layouts"]) == 2
    for layout in evidence["layouts"]:
        assert layout["planner_calls"] == 1
        assert layout["jev_logical_calls"] == layout["jev_transport_attempts"] == 2
        assert layout["verified_steps"] == 2
        assert layout["paid_api_calls"] == layout["external_network_requests"] == 0
        assert layout["first_button_bounds"] != layout["final_button_bounds"]
        assert (tmp_path / f"{layout['initial_layout']}-after.png").is_file()


def test_browser_focus_loss_blocks_input():
    class Page:
        def is_closed(self):
            return False

    screen = BrowserScreen(Page())
    screen.focused = False
    with pytest.raises(RuntimeError, match="lost focus"):
        screen.click_point(20, 20)
    assert screen.inputs == []
