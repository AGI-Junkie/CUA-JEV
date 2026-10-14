"""Exercise Windows branches with explicit synthetic window and screen evidence."""

from types import SimpleNamespace

import pytest
from test_desktop_contracts import task as desktop_task
from test_open_computer import environment
from test_open_desktop import FakeWindow

import cua_jev.open_computer as open_computer
import cua_jev.open_desktop as open_desktop
from cua_jev.executors.screen import ScreenController


@pytest.mark.parametrize("provider", ["desktop", "computer"])
def test_synthetic_uia_fixture_never_contacts_host_window_api(tmp_path, monkeypatch, provider):
    # Replace each module's sys binding, leaving pathlib/pytest's real platform
    # untouched. This exercises the same capture path that failed on Windows CI.
    windows = SimpleNamespace(platform="win32")
    monkeypatch.setattr(open_desktop, "sys", windows)
    monkeypatch.setattr(open_computer, "sys", windows)

    def reject_host_access(*args, **kwargs):
        pytest.fail("synthetic HWND fixture reached the real host screen controller")

    monkeypatch.setattr(ScreenController, "capture_window_lease", reject_host_access)
    monkeypatch.setattr(ScreenController, "ensure_foreground", reject_host_access)
    if provider == "desktop":
        task = desktop_task(FakeWindow())
        scene = task.observe(())
        desktop = task
        assert scene.state["scene_state"]["window_evidence"]["process_id"] == 123
    else:
        task = environment(tmp_path)
        task.reset()
        task.observe(())
        desktop = task.desktop
    assert desktop._window_lease.handle == 42
    assert desktop._window_lease.process_id == 123
    desktop.screen.validate_window_lease(desktop._window_lease)
    task.close()
