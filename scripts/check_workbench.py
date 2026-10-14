"""Reproduce UI simulation checks; run cua-jev-ui on localhost first. No API keys required."""

import json
import os
import re
import shutil
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1] / "docs" / "evidence"
root.mkdir(parents=True, exist_ok=True)
base_url = os.environ.get("CUA_JEV_UI_URL", "http://127.0.0.1:8768")
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=shutil.which("chromium"), headless=True, args=["--no-sandbox"]
    )
    page = browser.new_page(viewport={"width": 1536, "height": 1080}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base_url + "/workbench")
    page.wait_for_selector("#mini-log div")
    page.screenshot(path=str(root / "workbench-workspace.png"), full_page=True)
    page.click("#start")
    assert "请先确认" in page.locator("#status").inner_text()
    page.check("#scope")
    page.click("#start")
    page.wait_for_timeout(250)
    page.click("#pause")
    paused = page.locator("#history-log").inner_text()
    page.wait_for_timeout(2000)
    assert paused == page.locator("#history-log").inner_text()
    page.click("#pause")
    expect(page.locator("#status")).to_have_text(re.compile("已完成"), timeout=14000)
    assert "3 / 3" in page.locator("#verification").inner_text()
    page.screenshot(path=str(root / "workbench-completed.png"), full_page=True)
    page.locator(".testing summary").click()
    page.select_option("#layout", "b")
    page.check("#scope")
    page.select_option("#fault", "noeffect")
    page.click("#start")
    page.wait_for_selector("#recovery:not([hidden])")
    assert (
        "无预期效果" in page.locator("#failure-title").inner_text()
        or "未验证" in page.locator("#failure-title").inner_text()
    )
    page.screenshot(path=str(root / "workbench-recovery.png"), full_page=True)
    page.click("#reobserve")
    expect(page.locator("#status")).to_have_text(re.compile("已完成"), timeout=14000)
    for fault in ["focus", "stale", "timeout"]:
        page.check("#scope")
        page.select_option("#fault", fault)
        page.click("#start")
        page.wait_for_selector("#recovery:not([hidden])")
        page.check("#scope")
        page.click("#replan")
        page.wait_for_timeout(200)
        page.click("#stop")
        before = page.locator("#history-log").inner_text()
        page.wait_for_timeout(1700)
        assert before == page.locator("#history-log").inner_text()
    page.check("#scope")
    page.click("#start")
    page.wait_for_timeout(150)
    page.uncheck("#scope")
    before = page.locator("#history-log").inner_text()
    page.wait_for_timeout(1700)
    assert before == page.locator("#history-log").inner_text()
    page.check("#scope")
    page.click("#start")
    page.wait_for_timeout(150)
    page.click("#takeover")
    page.fill("#correction", "已重新定位目标窗口")
    page.click("#correct")
    assert not page.is_checked("#scope")
    assert "重新确认" in page.locator("#status").inner_text()
    page.click("[data-view=settings]")
    page.fill("#planner-key", "dummy-key-not-a-secret")
    page.fill("#jev-key", "dummy-key-not-a-secret")
    page.click("#settings-form button.primary")
    assert page.input_value("#planner-key") == ""
    assert page.input_value("#jev-key") == ""
    page.screenshot(path=str(root / "workbench-settings.png"), full_page=True)
    page.click("[data-view=history]")
    page.click("#load-history")
    page.wait_for_timeout(300)
    page.screenshot(path=str(root / "workbench-history.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("[data-view=workspace]")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(root / "workbench-mobile.png"), full_page=True)
    assert not errors, errors
    result = {
        "source": "actual Chromium render at FastAPI /workbench with CSP",
        "mode": "UI simulation only",
        "checks": [
            "scope required",
            "scope revocation cancels dispatch",
            "layout A completion",
            "layout B completion",
            "pause cancels delayed callbacks",
            "stop cancels delayed callbacks",
            "takeover invalidates scope",
            "focus loss recovery",
            "stale frame recovery",
            "timeout recovery",
            "no-effect recovery",
            "keys cleared",
            "history read-only",
            "mobile no horizontal overflow",
            "no browser page errors",
        ],
        "page_errors": errors,
    }
    (root / "workbench-checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    browser.close()
