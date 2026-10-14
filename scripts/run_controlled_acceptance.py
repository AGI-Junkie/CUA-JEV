"""Run real Chromium inputs with explicitly simulated planner/Jev responses.

No paid API, credentials, screenshot upload, or external browser requests.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import httpx

from cua_jev.controlled_browser import BrowserScreen, BrowserWindow
from cua_jev.guard import ActionGuard
from cua_jev.open_desktop import DesktopPlan, OpenDesktopTask
from cua_jev.policy import JevPolicy
from cua_jev.registry import ExecutorRegistry
from cua_jev.runtime import AgentRuntime
from cua_jev.trace import JsonlTrace
from cua_jev.verify import VerifierRegistry

ROOT = Path(__file__).resolve().parents[1]
NOTE = "Cloud acceptance note"


class SimulatedPlanner:
    """Test response fixture, not general-purpose planning intelligence."""
    def __init__(self) -> None:
        self.calls = 0
        self.latency_ms = 0.0

    def plan(self, goal, snapshot, recent_actions):
        started = time.perf_counter()
        self.calls += 1
        by_name = {control.name: control.ref for control in snapshot.controls}
        result = DesktopPlan.from_dict({
            "subgoal": "Write and save the local note",
            "options": [
                {"ref": by_name["Note text"], "operation": "fill", "value": NOTE},
                {"ref": by_name["Save local note"], "operation": "click",
                 "expected": {"kind": "text_contains", "value": f"Saved locally: {NOTE}"}},
            ],
            "success": {"kind": "text_contains", "value": f"Saved locally: {NOTE}"},
        }, snapshot)
        self.latency_ms += (time.perf_counter() - started) * 1000
        return result


def run_layout(browser, layout: str, output: Path) -> dict:
    page = browser.new_page(viewport={"width": 1180, "height": 940})
    # The fixture is local, and any accidental external request is blocked.
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:")
               else route.abort())
    fixture = ROOT / "fixtures/controlled_browser/task.html"
    page.set_content(fixture.read_text(encoding="utf-8"))
    page.evaluate("layout => document.body.dataset.layout = layout", layout)
    screen = BrowserScreen(page)
    planner = SimulatedPlanner()
    task = OpenDesktopTask(goal=f"Save local note {NOTE}", window_title_re="Local task lab",
                           planner=planner, window=BrowserWindow(page), screen=screen,
                           allow_text_input=True, allow_button_actions=True,
                           contract_path=output / f"{layout}-plan.json")
    requests = []

    def simulated_jev(request):
        body = json.loads(request.content)
        actions = body["state"]["available_actions"]
        # A test response fixture selects GUI among legal candidates. Production
        # JevPolicy parses/validates it, never calls RulePolicy or another model.
        selected = next((a for a in actions if a["channel"] == "gui"), actions[0])
        requests.append({"offered": [a["id"] for a in actions], "selected": selected["id"]})
        return httpx.Response(200, json={"model": "simulated-jev-acceptance", "answers": {"action": {
            "choice": selected["id"], "confidence": 1.0,
            "probabilities": {a["id"]: float(a["id"] == selected["id"]) for a in actions},
        }}})

    client = httpx.Client(transport=httpx.MockTransport(simulated_jev))
    policy = JevPolicy(api_key="non-secret-local-fixture", client=client, retries=0)
    executors = ExecutorRegistry()
    for channel, executor in task.executor_bindings().items():
        executors.register(channel, executor)
    verifiers = VerifierRegistry()
    task.register_verifiers(verifiers)
    trace_path = output / f"{layout}.jsonl"
    trace_path.write_text("", encoding="utf-8")
    trace = JsonlTrace(trace_path)
    runtime = AgentRuntime(policy=policy, guard=ActionGuard(allow_writes=True, allow_destructive=True),
                           executors=executors, verifiers=verifiers, trace=trace,
                           require_jev=True, decision_timeout_s=5)
    history = []
    task.reset()
    page.screenshot(path=str(output / f"{layout}-before.png"), full_page=True)
    first_bounds = None
    started = time.perf_counter()
    for index in range(4):
        observation = task.observe(history)
        candidates = task.candidates(observation, history)
        if index == 0:
            first_bounds = next(c.rectangle for c in task._snapshot.controls if c.name == "Save local note")
        step = runtime.step(observation, candidates)
        history.append(step)
        envelope = runtime.last_jev_decision
        assert envelope is not None
        assert envelope.decision.decision_id == step.receipt.decision_id
        assert envelope.plan_contract_id == task.plan_contract.contract_id
        assert envelope.decision.candidate_id in envelope.offered_candidate_ids
        assert envelope.request_hash
        candidate = next(c for c in candidates if c.id == step.decision.candidate_id)
        evaluation = task.evaluate(observation, candidate, step.receipt, step.verification)
        assert step.receipt.success, step.receipt.error
        assert step.verification.passed, step.verification.details
        if index == 0:
            # Change geometry and DOM ordering between actions. The persistent
            # contract must freshly bind its second action; old refs/coords fail.
            page.evaluate("""() => {
                document.body.dataset.layout = document.body.dataset.layout === 'columns'
                    ? 'stacked' : 'columns';
                document.querySelector('main').append(document.querySelector('main section'));
            }""")
        if evaluation.success:
            break
    actual = page.locator('[role=status]').inner_text()
    assert actual == f"Saved locally: {NOTE}", actual
    assert planner.calls == 1, "layout changes must reuse one persistent plan"
    assert len(history) == 2, "expected independently verified fill and click"
    assert policy.metrics["calls"] == len(history)
    assert policy.metrics["attempts"] == len(requests)
    assert policy.metrics["latency_ms"] > 0
    assert runtime.metrics["policy_calls"] == len(history)
    final_bounds = next(c.rectangle for c in task.capture_state().controls if c.name == "Save local note")
    assert final_bounds != first_bounds
    page.screenshot(path=str(output / f"{layout}-after.png"), full_page=True)
    result = {"initial_layout": layout, "layout_changed_between_actions": True,
              "first_button_bounds": first_bounds, "final_button_bounds": final_bounds,
              "browser": "real headless Chromium", "observer": "DOM semantics stand in for UIA; not VLM",
              "models": "simulated planner and Jev HTTP responses; no paid calls",
              "planner_calls": planner.calls, "planner_latency_ms": round(planner.latency_ms, 3),
              "jev_logical_calls": len(history), "jev_transport_attempts": len(requests),
              "jev_latency_ms": [round(s.decision.latency_ms, 3) for s in history],
              "policy_metrics": policy.metrics.copy(), "runtime_metrics": runtime.metrics.copy(),
              "require_jev": True, "decision_timeout_s": 5,
              "plan_contract_file": f"{layout}-plan.json",
              "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
              "verified_steps": len(history), "independent_dom_postcondition": actual,
              "mouse_keyboard_inputs": screen.inputs, "api_choices": requests,
              "external_network_requests": 0, "paid_api_calls": 0}
    client.close()
    page.close()
    return result


def run(output: Path) -> dict:
    from playwright.sync_api import sync_playwright
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"),
                                             headless=True, args=["--no-sandbox"])
        try:
            result = {"scope": "controlled local fixture only; not arbitrary Windows/apps/games",
                      "layouts": [run_layout(browser, layout, output) for layout in ("columns", "stacked")]}
        finally:
            browser.close()
    (output / "evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".runs/controlled-acceptance")
    args = parser.parse_args()
    print(json.dumps(run(args.output), indent=2))
