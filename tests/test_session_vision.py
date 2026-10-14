"""Screenshot API integration with simulated inference and real local Chromium."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil

import pytest

from cua_jev.ui.configuration import ModelRoleConfig
from cua_jev.ui.session_vision import create_mock_vision
from cua_jev.vision import WindowImage


def test_mock_vision_rejects_invalid_image_and_reserves_before_transport():
    attempts, events = [], []
    planner, client = create_mock_vision(
        None,
        ModelRoleConfig(
            name="configured-vision", endpoint="https://unused.example/v1", api_key="must-never-leave-process"
        ),
        attempts.append,
        lambda *event: events.append(event),
    )
    try:
        # WindowImage's container is valid, but this is not a decodable screenshot.
        with pytest.raises(RuntimeError, match="vision request failed"):
            planner.perceive_scene(
                "Local test",
                "Local",
                "",
                WindowImage(b"\xff\xd8not-a-real-jpeg", bytes(64 * 64), (0, 0, 100, 100)),
            )
        assert attempts == ["vision"]
        assert events == []
        assert planner.api_key is None
        assert planner.model == "configured-vision"
        assert planner.base_url == "https://unused.example/v1"
    finally:
        client.close()


def test_mock_vision_budget_rejection_precedes_image_or_dom_work():
    def reject(role):
        assert role == "vision"
        raise RuntimeError("attempt budget exhausted")

    planner, client = create_mock_vision(None, ModelRoleConfig(), reject, lambda *_args: None)
    try:
        with pytest.raises(RuntimeError, match="budget exhausted"):
            planner.perceive_scene(
                "test", "test", "", WindowImage(b"\xff\xd8empty", bytes(64 * 64), (0, 0, 100, 100))
            )
    finally:
        client.close()


@pytest.mark.skipif(
    not shutil.which("chromium") or importlib.util.find_spec("playwright") is None,
    reason="requires installed local Chromium and playwright; no downloads",
)
def test_actual_jpeg_api_roundtrip_and_fresh_visual_targets_after_layout_change():
    from PIL import Image
    from playwright.sync_api import sync_playwright

    from cua_jev.controlled_browser import BrowserScreen

    attempts, events = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("chromium"), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 700})
            page.route("**/*", lambda route: route.abort())
            page.set_content("""<title>Controlled screenshot lab</title>
              <style>main {display:flex;gap:200px;margin:50px} button {width:140px;height:60px}</style>
              <h1>Local note</h1><main><input aria-label="Note"><button>Save local note</button></main>
              <p role="status">Waiting for local input</p><input type="password" aria-label="Password">
              <button style="display:none">Invisible</button>""")
            planner, client = create_mock_vision(
                page,
                ModelRoleConfig(
                    name="vision-role-choice",
                    endpoint="https://not-contacted.example/v1",
                    api_key="private-key-example",
                ),
                attempts.append,
                lambda kind, data: events.append({"type": kind, "data": data}),
            )
            try:
                screen = BrowserScreen(page)
                first_image = screen.capture_region((0, 0, 1000, 700))
                first = planner.perceive_scene("Save local note", page.title(), "", first_image)
                first_save = next(target for target in first.targets if target.label == "Save local note")
                assert {target.label for target in first.targets} == {"Note", "Save local note"}
                assert "Waiting for local input" in first.visible_text
                page.locator("main").evaluate("e => e.style.flexDirection = 'column'")
                second_image = screen.capture_region((0, 0, 1000, 700))
                second = planner.perceive_scene("Save local note", page.title(), "", second_image)
                second_save = next(target for target in second.targets if target.label == "Save local note")
                assert first_save.box != second_save.box
                assert first_image.jpeg != second_image.jpeg
                assert attempts == ["vision", "vision"]
                assert planner.vision_calls == planner.vision_attempts == 2
                assert events[0]["data"]["pixel_sha256"] != events[1]["data"]["pixel_sha256"]
                metadata = json.dumps(events)
                assert "private-key-example" not in metadata
                assert "data:image" not in metadata
                assert all(event["data"]["simulated"] for event in events)
                assert all(not event["data"]["external_network"] for event in events)
                assert all(event["data"]["model"] == "vision-role-choice" for event in events)
                with Image.open(io.BytesIO(first_image.jpeg)) as decoded:
                    assert events[0]["data"]["image_size"] == list(decoded.size)
            finally:
                client.close()
        finally:
            browser.close()
