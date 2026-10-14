"""Browser checks against the actual controlled-browser session API. No paid models.

Start cua-jev-ui first. This creates local notes and captures real UI/screenshots;
model responses remain simulated HTTP parsed by the real model adapters.
"""

import json
import os
import shutil
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1] / "docs" / "evidence"
root.mkdir(parents=True, exist_ok=True)
base_url = os.environ.get("CUA_JEV_UI_URL", "http://127.0.0.1:8768")
checks = []
sessions = []
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=shutil.which("chromium"), headless=True, args=["--no-sandbox"]
    )
    page = browser.new_page(viewport={"width": 1536, "height": 1080}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base_url + "/workbench")
    expect(page.locator("#status")).to_have_text("先创建真实浏览器会话")
    assert page.locator("#start").is_disabled()
    checks.append("no dispatch before real browser preview")
    page.click("[data-view=settings]")
    page.fill("#planner-key", "local-test-key-no-provider-access")
    page.fill("#jev-key", "local-test-key-no-provider-access")
    page.fill("#budget", "8")
    page.click("#settings-form button.primary")
    expect(page.locator("#settings-status")).to_contain_text("后端已确认")
    assert page.input_value("#planner-key") == page.input_value("#jev-key") == ""
    checks.append("backend configuration ack and password inputs cleared")
    page.screenshot(path=str(root / "workbench-settings.png"), full_page=True)
    # Explicitly remove temporary process-only test keys after proving safe round-trip.
    page.check("#planner-clear")
    page.check("#jev-clear")
    page.click("#settings-form button.primary")
    expect(page.locator("#planner-key-status")).to_contain_text("未配置密钥")
    page.click("[data-view=workspace]")
    for layout in ("columns", "stacked"):
        note = f"UI bridge actual {layout} note"
        page.select_option("#layout", layout)
        page.fill("#note-text", note)
        page.fill("#context", f"Local only; layout {layout}; no external actions")
        page.click("#preview")
        expect(page.locator("#status")).to_have_text("请确认受控浏览器视口范围", timeout=20000)
        expect(page.locator("#screen")).to_be_visible()
        assert page.locator("#screen").evaluate("e=>e.naturalWidth") > 0
        src = page.locator("#screen").get_attribute("src")
        assert src.startswith("/api/workbench/sessions/") and "/frame" in src
        session_id = src.split("/")[4]
        sessions.append(session_id)
        checks.append(f"{layout}: actual backend PNG preview")
        page.click("#start")
        expect(page.locator("#status")).to_contain_text("请先查看真实截图")
        if layout == "columns":
            page.screenshot(path=str(root / "workbench-workspace.png"), full_page=True)
        page.check("#scope")
        page.click("#start")
        expect(page.locator("#status")).to_have_text("已完成 · 后置条件通过", timeout=25000)
        expect(page.locator("#verification")).to_have_text("预期效果通过")
        expect(page.locator("#calls")).to_have_text("1 / 2 次（模拟 API）")
        state = page.request.get(base_url + f"/api/workbench/sessions/{session_id}").json()
        batch = page.request.get(base_url + f"/api/workbench/sessions/{session_id}/events").json()
        assert state["status"] == "completed" and state["counters"]["steps"] == 2
        assert state["counters"]["attempts_by_role"] == {"planner": 1, "jev": 2}
        assert any(
            e["type"] == "completion" and e["data"]["actual"] == f"Saved locally: {note}"
            for e in batch["events"]
        )
        assert any(e["type"] == "receipt" and e["data"]["success"] for e in batch["events"])
        assert any(e["type"] == "verification" and e["data"]["passed"] for e in batch["events"])
        checks.append(f"{layout}: real GUI receipt, exact note verification, measured API counters")
        page.screenshot(path=str(root / f"workbench-completed-{layout}.png"), full_page=True)
    page.click("[data-view=settings]")
    page.fill("#budget", "1")
    page.click("#settings-form button.primary")
    expect(page.locator("#settings-status")).to_contain_text("后端已确认")
    page.click("[data-view=workspace]")
    page.click("#preview")
    expect(page.locator("#status")).to_have_text("请确认受控浏览器视口范围", timeout=20000)
    page.check("#scope")
    page.click("#start")
    expect(page.locator("#recovery")).to_be_visible(timeout=25000)
    expect(page.locator("#failure-detail")).to_contain_text("Step budget exhausted")
    page.screenshot(path=str(root / "workbench-recovery.png"), full_page=True)
    checks.append("real backend action-budget failure shows recovery, never completion")
    page.click("#stop")
    expect(page.locator("#status")).to_have_text("后端已停止", timeout=10000)
    page.click("[data-view=settings]")
    page.fill("#budget", "8")
    page.click("#settings-form button.primary")
    expect(page.locator("#settings-status")).to_contain_text("后端已确认")
    page.click("[data-view=workspace]")
    # Exercise acknowledged scope revocation before starting a fresh live browser session.
    page.click("#preview")
    expect(page.locator("#status")).to_have_text("请确认受控浏览器视口范围", timeout=20000)
    src = page.locator("#screen").get_attribute("src")
    session_id = src.split("/")[4]
    sessions.append(session_id)
    page.check("#scope")
    # Confirm only via backend, so there is no timing race with this two-step task completing.
    csrf = page.request.get(base_url + "/api/bootstrap").json()["csrf"]
    headers = {"X-CUA-JEV-CSRF": csrf}
    response = page.request.post(
        base_url + f"/api/workbench/sessions/{session_id}/confirm", headers=headers, data={}
    )
    assert response.ok
    expect(page.locator("#status")).to_have_text("范围已确认", timeout=10000)
    page.uncheck("#scope")
    expect(page.locator("#status")).to_have_text("请确认受控浏览器视口范围", timeout=10000)
    state = page.request.get(base_url + f"/api/workbench/sessions/{session_id}").json()
    assert not state["scope_confirmed"]
    checks.append("scope revocation acknowledged by backend")
    # Put the actual worker into paused state at its initial running transition,
    # then exercise takeover and correction through the UI without timing guesses.
    page.request.post(base_url + f"/api/workbench/sessions/{session_id}/confirm", headers=headers, data={})
    for _ in range(100):
        state = page.request.get(base_url + f"/api/workbench/sessions/{session_id}").json()
        if state["status"] == "ready" and not state.get("command_pending"):
            break
        time.sleep(0.02)
    page.request.post(base_url + f"/api/workbench/sessions/{session_id}/start", headers=headers, data={})
    for _ in range(100):
        state = page.request.get(base_url + f"/api/workbench/sessions/{session_id}").json()
        if state["status"] == "running":
            break
        time.sleep(0.01)
    response = page.request.post(
        base_url + f"/api/workbench/sessions/{session_id}/pause", headers=headers, data={}
    )
    assert response.ok
    expect(page.locator("#status")).to_have_text("后端已暂停", timeout=10000)
    page.reload()
    expect(page.locator("#status")).to_have_text("后端已暂停", timeout=10000)
    expect(page.locator("#scope-text")).to_contain_text(session_id)
    expect(page.locator("#history-log")).not_to_be_empty()
    checks.append("reload restores authoritative active session, events and controls")
    page.fill("#context", "changed paused context")
    page.locator(".stage-head h2").click()
    page.wait_for_timeout(1100)
    assert page.locator("#start").is_disabled()
    assert page.locator("#pause").is_disabled()
    assert page.locator("#scope").is_disabled()
    checks.append("dirty paused task stays blocked across polling until correction/new preview")
    page.click("#takeover")
    expect(page.locator("#status")).to_have_text("后端已交还控制", timeout=10000)
    page.screenshot(path=str(root / "workbench-takeover.png"), full_page=True)
    original_task = page.input_value("#task")
    page.fill("#task", "unsupported task edit must not replace the active contract")
    page.select_option("#observe-mode", "mock_vision")
    page.fill("#correction", "attempt unsupported field correction")
    page.click("#correct")
    expect(page.locator("#correction-hint")).to_contain_text("请创建新会话")
    page.wait_for_timeout(700)
    assert page.locator("#start").is_disabled()
    state = page.request.get(base_url + f"/api/workbench/sessions/{session_id}").json()
    assert state["task"] == original_task
    checks.append("unsupported task/observer correction cannot silently resume stale backend task")
    page.fill("#task", original_task)
    page.select_option("#observe-mode", "dom")
    page.fill("#note-text", "Corrected live browser note")
    page.fill("#correction", "改为保存纠正后的文本；重新确认窗口")
    page.click("#correct")
    expect(page.locator("#scope")).not_to_be_checked()
    page.check("#scope")
    page.click("#start")
    expect(page.locator("#status")).to_have_text("已完成 · 后置条件通过", timeout=25000)
    checks.append("real paused worker takeover, correction, scope reconfirmation, resume completion")
    page.click("#preview")
    expect(page.locator("#status")).to_have_text("请确认受控浏览器视口范围", timeout=20000)
    page.click("#stop")
    expect(page.locator("#status")).to_have_text("后端已停止", timeout=10000)
    checks.append("emergency stop waits for real backend stopped state")
    page.select_option("#observe-mode", "mock_vision")
    page.select_option("#layout", "columns")
    page.fill("#note-text", "Actual screenshot mocked vision note")
    page.click("#preview")
    expect(page.locator("#status")).to_have_text("请确认受控浏览器视口范围", timeout=20000)
    src = page.locator("#screen").get_attribute("src")
    visual_id = src.split("/")[4]
    sessions.append(visual_id)
    page.check("#scope")
    page.click("#start")
    expect(page.locator("#status")).to_have_text("已完成 · 后置条件通过", timeout=25000)
    state = page.request.get(base_url + f"/api/workbench/sessions/{visual_id}").json()
    batch = page.request.get(base_url + f"/api/workbench/sessions/{visual_id}/events").json()
    assert state["counters"]["vision"]["calls"] > 0
    assert any(
        e["type"] == "receipt" and e["data"].get("capability") == "desktop.visual_click"
        for e in batch["events"]
    )
    assert "simulated" in state["capabilities"]["observer"].lower()
    page.screenshot(path=str(root / "workbench-completed-vision.png"), full_page=True)
    checks.append("optional actual screenshot / simulated vision API / real visual-click receipt")
    page.click("[data-view=history]")
    page.click("#load-history")
    expect(page.locator("#backend-history")).to_contain_text(session_id)
    page.screenshot(path=str(root / "workbench-history.png"), full_page=True)
    checks.append("backend facts/history shown read-only")
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("[data-view=workspace]")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(root / "workbench-mobile.png"), full_page=True)
    assert not errors, errors
    checks.extend(["mobile no horizontal overflow", "no browser page errors"])
    result = {
        "source": "actual Chromium UI at FastAPI /workbench, CSP enabled",
        "execution": "real controlled Chromium GUI input and exact DOM verification",
        "models": "simulated HTTP via ChatModelPlanner and JevPolicy; no paid models",
        "windows": False,
        "checks": checks,
        "session_ids": sessions,
        "page_errors": errors,
    }
    (root / "workbench-checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    browser.close()
