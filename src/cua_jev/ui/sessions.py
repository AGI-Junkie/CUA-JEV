"""Real local Chromium task sessions; only external model HTTP responses are fixtures.

All Playwright objects belong to one worker. No browser reference escapes it.
The lab is intentionally bounded: save a user-supplied note, no arbitrary sites.
"""

from __future__ import annotations

import copy
import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from ..controlled_browser import BrowserScreen, BrowserWindow
from ..guard import ActionGuard
from ..model_planner import ChatModelPlanner
from ..open_desktop import OpenDesktopTask
from ..policy import JevPolicy
from ..registry import ExecutorRegistry
from ..runtime import AgentRuntime
from ..safety import RuntimeControl
from ..trace import JsonlTrace
from ..verify import VerifierRegistry

TERMINAL = {"stopped", "completed", "failed"}


class _SessionTrace(JsonlTrace):
    def __init__(self, session):
        super().__init__(session.directory / "trace.jsonl", run_id=session.id)
        self.session = session

    def append(self, kind, payload):
        super().append(kind, payload)
        self.session.event(kind, payload)
        if kind in {"decision", "jev_decision"}:
            self.session.update(current_action={"stage": "proposed", **payload})
        elif kind == "receipt":
            self.session.update(
                current_action={
                    "stage": "input_delivered" if payload.get("success") else "input_failed",
                    **payload,
                }
            )
        elif kind == "verification":
            self.session.update(
                current_action={"stage": "verified" if payload.get("passed") else "unverified", **payload}
            )


class ControlledSessionManager:
    def __init__(self, root: str | Path, data: str | Path | None = None, configuration=None):
        self.root = Path(root)
        self.directory = Path(data or self.root / ".runs" / "workbench")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.configuration = configuration
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        # Recovered records are history only. A browser cannot be restored from JSON.
        self._history = {}
        for path in self.directory.glob("*/session.json"):
            try:
                state = json.loads(path.read_text())
                if state["status"] not in TERMINAL:
                    state.update(status="interrupted", error="Server restarted; create a new scoped session")
                self._history[state["id"]] = state
            except (ValueError, KeyError, OSError):
                continue

    def create(self, payload: dict[str, Any]):
        if not isinstance(payload, dict):
            raise ValueError("session body must be an object")
        allowed = {"task", "context", "note_text", "layout", "mode"}
        if self.__class__.__name__ == "TestSessionManager":
            allowed.add("_test_faults")
        if set(payload) - allowed:
            raise ValueError("Unsupported session fields; only the controlled note-save lab is available")
        if payload.get("mode", "controlled_mock") != "controlled_mock":
            raise ValueError(
                "External model execution is disabled in this controlled lab; no API consent is inferred"
            )
        for key in ("task", "note_text"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(f"{key} is required")
        if len(payload["task"]) > 2000 or len(payload["note_text"]) > 120:
            raise ValueError("task must be <=2000 characters and note_text <=120 characters")
        if not isinstance(payload.get("context", ""), str) or len(payload.get("context", "")) > 4000:
            raise ValueError("context must be text <=4000 characters")
        if payload.get("layout", "columns") not in {"columns", "stacked"}:
            raise ValueError("unsupported controlled layout")
        with self._lock:
            if any(s.state["status"] not in TERMINAL for s in self._sessions.values()):
                raise ValueError(
                    "An active session exists; stop it and wait for acknowledgement before switching"
                )
            config = self.configuration.snapshot() if self.configuration is not None else None
            session = _Session(self, payload, config)
            self._sessions[session.id] = session
            session.thread.start()
            return session.public()

    def get(self, session_id):
        if session_id in self._sessions:
            return self._sessions[session_id].public()
        if session_id in self._history:
            return copy.deepcopy(self._history[session_id])
        raise KeyError(session_id)

    def list(self):
        return [self.get(i) for i in {**self._history, **self._sessions}]

    def command(self, session_id, action, payload=None):
        with self._lock:
            if session_id not in self._sessions:
                raise ValueError("Historical sessions cannot resume; create a new session")
            return self._sessions[session_id].command(action, payload or {})

    def events(self, session_id, after=0):
        if session_id not in self._sessions:
            self.get(session_id)
            path = self.directory / session_id / "events.jsonl"
            events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        else:
            with self._sessions[session_id].lock:
                events = copy.deepcopy(self._sessions[session_id].events)
        return {
            "events": [e for e in events if e["seq"] > after],
            "last_seq": events[-1]["seq"] if events else 0,
        }

    def frame_path(self, session_id):
        self.get(session_id)
        path = self.directory / session_id / "frame.png"
        if not path.is_file():
            raise FileNotFoundError("Preview frame is not ready")
        return path

    def close(self):
        for session in list(self._sessions.values()):
            if session.public()["status"] not in TERMINAL:
                session.command("stop", {})
        for session in self._sessions.values():
            session.thread.join(timeout=8)


class _Session:
    def __init__(self, manager, payload, configuration):
        self.manager, self.configuration = manager, configuration
        self.id = uuid.uuid4().hex
        self.directory = manager.directory / self.id
        self.directory.mkdir(mode=0o700)
        self.lock = threading.RLock()
        self.commands = queue.Queue()
        self.control = RuntimeControl()
        self.control.pause()
        self.events = []
        self.state = {
            "id": self.id,
            "status": "preparing",
            "task": payload["task"].strip(),
            "context": payload.get("context", ""),
            "note_text": payload["note_text"],
            "layout": payload.get("layout", "columns"),
            "mode": "controlled_mock",
            "scope_confirmed": False,
            "scene": None,
            "plan": None,
            "current_action": None,
            "counters": {},
            "error": None,
            "frame_url": None,
            "capabilities": {
                "browser": "real headless Chromium",
                "observer": "DOM semantics; not VLM",
                "models": "simulated HTTP responses parsed by ChatModelPlanner and JevPolicy",
                "scope": "local note-save lab only",
                "external_network": False,
                "drag": False,
                "windows": False,
                "cost": "No paid calls; real API cost unknown",
            },
        }
        # Faults are test-only HTTP fixture inputs, bounded and never external.
        self.faults = (
            payload.get("_test_faults", {}) if manager.__class__.__name__ == "TestSessionManager" else {}
        )
        self.thread = threading.Thread(target=self.run, daemon=True, name=f"lab-{self.id[:8]}")
        self.event("lifecycle", {"status": "preparing"})

    def public(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def update(self, **values):
        with self.lock:
            self.state.update(values)
            temporary = self.directory / "session.tmp"
            temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.directory / "session.json")

    def event(self, kind, data):
        with self.lock:
            event = {
                "seq": len(self.events) + 1,
                "type": kind,
                "at": time.time(),
                "data": copy.deepcopy(data),
            }
            self.events.append(event)
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")

    def status(self, status, **values):
        self.update(status=status, **values)
        self.event("lifecycle", {"status": status, **values})

    def command(self, action, payload):
        with self.lock:
            status = self.state["status"]
            if not isinstance(payload, dict) or (action != "correct" and payload):
                raise ValueError("Only correct accepts command fields")
            allowed = {
                "confirm_scope": {"scope_ready", "takeover", "paused"},
                "start": {"ready"},
                "pause": {"running"},
                "takeover": {"paused"},
                "correct": {"paused", "takeover", "scope_ready"},
                "resume": {"paused", "takeover", "ready"},
                "reobserve": {"paused", "takeover", "scope_ready", "ready"},
                "replan": {"paused", "takeover"},
                "invalidate_scope": {"paused", "takeover", "scope_ready", "ready"},
                "stop": set(self.state_statuses()) - TERMINAL,
            }
            if action not in allowed or status not in allowed[action]:
                raise ValueError(f"Cannot {action} while session is {status}")
            if self.state.get("command_pending") and action != "stop":
                raise ValueError("Wait for the pending command acknowledgement")
            if action in {"start", "resume"} and not self.state["scope_confirmed"]:
                raise ValueError("Confirm the current scope before starting or resuming")
            if action == "correct":
                if set(payload) - {"note_text", "context", "layout"}:
                    raise ValueError("Correction supports note_text, context and layout only")
                note = payload.get("note_text", self.state["note_text"])
                if not isinstance(note, str) or not note.strip() or len(note) > 120:
                    raise ValueError("note_text must contain 1-120 characters")
                if payload.get("layout", self.state["layout"]) not in {"columns", "stacked"}:
                    raise ValueError("unsupported layout")
                if not isinstance(payload.get("context", ""), str) or len(payload.get("context", "")) > 4000:
                    raise ValueError("context must be text <=4000 characters")
            if action == "correct":
                self.update(scope_confirmed=False)
            # Invalidate an outstanding model result now, before worker acknowledgement.
            if action in {"pause", "stop"}:
                getattr(self.control, action)()
                self.status("pausing" if action == "pause" else "stopping")
            elif action in {"start", "resume"}:
                self.status("starting")  # synchronously reject duplicate queued starts
            else:
                self.update(command_pending=action)
            self.commands.put((action, copy.deepcopy(payload)))
            return self.public()

    @staticmethod
    def state_statuses():
        return {
            "preparing",
            "scope_ready",
            "ready",
            "starting",
            "running",
            "pausing",
            "paused",
            "takeover",
            "stopping",
            "stopped",
            "completed",
            "failed",
        }

    def frame(self, page):
        temporary = self.directory / "frame.tmp.png"
        page.screenshot(path=str(temporary))
        temporary.replace(self.directory / "frame.png")
        version = len(self.events)
        self.update(frame_url=f"/api/workbench/sessions/{self.id}/frame?v={version}")

    def run(self):
        from playwright.sync_api import sync_playwright

        from .configuration import BudgetLedger, RunLimits

        limits = self.configuration.limits if self.configuration else RunLimits()
        ledger = None
        self.ledger = None
        browser = None
        clients = []
        history = []
        task = None
        failure = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=shutil.which("chromium"), headless=True, args=["--no-sandbox"]
                )
                page = browser.new_page(viewport={"width": 1180, "height": 940})
                page.route("**/*", lambda route: route.abort())
                page.set_content((self.manager.root / "fixtures/controlled_browser/task.html").read_text())
                page.evaluate("layout => document.body.dataset.layout = layout", self.state["layout"])
                screen = BrowserScreen(page)
                if self.faults.get("no_effect"):
                    page.evaluate("document.querySelector('#save').onclick = () => {}")
                if self.faults.get("lost_focus"):
                    screen.focused = False

                attempt_indices = {"planner": 0, "jev": 0}

                def handler(role, request):
                    ledger.record_model_attempt(role, retry=attempt_indices[role])
                    attempt_indices[role] += 1
                    delay = min(float(self.faults.get(f"{role}_delay", 0)), 5)
                    if delay:
                        time.sleep(delay)
                    ledger.check()
                    if self.faults.get(f"{role}_timeout"):
                        raise httpx.ReadTimeout("local fixture timeout", request=request)
                    body = json.loads(request.content)
                    if role == "planner":
                        supplied = json.loads(body["messages"][-1]["content"])
                        controls = {c["name"]: c["ref"] for c in supplied["snapshot"]["controls"]}
                        note = self.state["note_text"]
                        raw = {
                            "subgoal": "Fill and save the requested note in the controlled lab",
                            "options": [
                                {"ref": controls["Note text"], "operation": "fill", "value": note},
                                {
                                    "ref": controls["Save local note"],
                                    "operation": "click",
                                    "expected": {"kind": "text_contains", "value": f"Saved locally: {note}"},
                                },
                            ],
                            "success": {"kind": "text_contains", "value": f"Saved locally: {note}"},
                        }
                        self.event(
                            "model_request",
                            {
                                "role": role,
                                "mode": "simulated_http",
                                "model": body["model"],
                                "goal": supplied["goal"],
                            },
                        )
                        return httpx.Response(
                            200, json={"choices": [{"message": {"content": json.dumps(raw)}}]}
                        )
                    actions = body["state"]["available_actions"]
                    # The mock service returns a legal choice; production policy still parses it.
                    selected = next((a for a in actions if a["channel"] == "gui"), actions[0])
                    self.event(
                        "model_request",
                        {
                            "role": role,
                            "mode": "simulated_http",
                            "model": body["model"],
                            "offered": len(actions),
                        },
                    )
                    return httpx.Response(
                        200,
                        json={
                            "model": "simulated-jev-http",
                            "answers": {
                                "action": {
                                    "choice": selected["id"],
                                    "confidence": 1.0,
                                    "probabilities": {
                                        a["id"]: float(a["id"] == selected["id"]) for a in actions
                                    },
                                }
                            },
                        },
                    )

                planner_client = httpx.Client(transport=httpx.MockTransport(lambda r: handler("planner", r)))
                jev_client = httpx.Client(transport=httpx.MockTransport(lambda r: handler("jev", r)))
                roles = self.configuration.roles if self.configuration else {}
                planner_role = roles.get("planner")
                jev_role = roles.get("jev")
                clients.extend([planner_client, jev_client])
                planner = ChatModelPlanner(
                    planner_role.endpoint
                    if planner_role and planner_role.endpoint
                    else "https://local-fixture.invalid/v1",
                    planner_role.name if planner_role and planner_role.name else "simulated-planner-http",
                    api_key=planner_role.api_key if planner_role else None,
                    client=planner_client,
                )
                policy = JevPolicy(
                    api_key=jev_role.api_key if jev_role and jev_role.api_key else "non-secret-local-fixture",
                    model=jev_role.name if jev_role and jev_role.name else "simulated-jev-http",
                    api_url=jev_role.endpoint
                    if jev_role and jev_role.endpoint
                    else "https://local-fixture.invalid/jev",
                    client=jev_client,
                    retries=limits.max_retries,
                    sleep=lambda _: None,
                )

                def make_task():
                    goal = (
                        f"{self.state['task']}\nContext: {self.state['context']}\n"
                        "Controlled acceptance requirement: save this exact local note: "
                        f"{self.state['note_text']}"
                    )
                    return OpenDesktopTask(
                        goal=goal,
                        window_title_re="Local task lab",
                        planner=planner,
                        window=BrowserWindow(page),
                        screen=screen,
                        allow_text_input=True,
                        allow_button_actions=True,
                        contract_path=self.directory / "plan.json",
                    )

                task = make_task()
                task.reset()
                registry = ExecutorRegistry()
                verifiers = VerifierRegistry()

                runtime = None

                def bind_runtime():
                    for channel, executor in task.executor_bindings().items():

                        def bounded_execute(candidate, observation_id, decision_id, execute=executor):
                            ledger.record_step()
                            return execute(candidate, observation_id, decision_id)

                        registry.register(channel, bounded_execute)
                    task.register_verifiers(verifiers)
                    if runtime is not None:
                        return runtime  # Preserve the outstanding-policy gate across correction.
                    return AgentRuntime(
                        policy=policy,
                        guard=ActionGuard(allow_writes=True, allow_destructive=True),
                        executors=registry,
                        verifiers=verifiers,
                        trace=_SessionTrace(self),
                        control=self.control,
                        require_jev=True,
                        decision_timeout_s=limits.decision_timeout_seconds,
                    )

                runtime = bind_runtime()
                preview = task.observe([])
                self.update(scene=preview.state)
                self.event("observation", preview.to_dict())
                self.frame(page)
                if self.control.status != "stopped":
                    self.status("scope_ready")
                while True:
                    try:
                        action, payload = self.commands.get(timeout=0.03)
                    except queue.Empty:
                        action, payload = None, {}
                    if action:
                        if self.control.status == "stopped" and action != "stop":
                            continue
                        if action == "stop":
                            browser.close()
                            browser = None
                            self.status("stopped", command_pending=None)
                            return
                        if action == "pause":
                            self.status("paused")
                        elif action == "takeover":
                            self.status("takeover")
                        elif action == "invalidate_scope":
                            self.control.pause()
                            self.update(scope_confirmed=False)
                            self.status("scope_ready")
                        elif action == "confirm_scope":
                            self.frame(page)
                            self.update(scope_confirmed=True)
                            self.status("ready" if not history else "paused")
                        elif action == "correct":
                            self.update(**payload, scope_confirmed=False, plan=None, error=None)
                            page.evaluate(
                                "layout => document.body.dataset.layout = layout", self.state["layout"]
                            )
                            task = make_task()
                            task.reset()
                            history = []
                            runtime = bind_runtime()
                            preview = task.observe([])
                            self.update(scene=preview.state)
                            self.event("observation", preview.to_dict())
                            self.frame(page)
                            self.status("takeover")
                        elif action in {"reobserve", "replan"}:
                            if action == "replan":
                                task.reset()
                            observation = task.observe(history)
                            self.update(scene=observation.state, error=None)
                            self.event("observation", observation.to_dict())
                            self.frame(page)
                        elif action in {"start", "resume"}:
                            if not self.public()["scope_confirmed"]:
                                self.status("scope_ready", error="Scope confirmation required")
                                continue
                            if ledger is None:
                                ledger = BudgetLedger(
                                    limits,
                                    cancelled=lambda: self.control.status != "running",
                                    roles=self.configuration.roles if self.configuration else {},
                                )
                                self.ledger = ledger
                            self.control.resume()
                            self.status("running", error=None)
                        self.update(command_pending=None)
                    if self.public()["status"] != "running":
                        continue
                    try:
                        generation = self.control.token()
                        ledger.check()
                        observation = task.observe(history)
                        self.update(scene=observation.state)
                        self.frame(page)
                        attempt_indices["planner"] = 0
                        candidates = task.candidates(observation, history)
                        if task.plan_contract:
                            self.update(plan=task.plan_contract.to_dict())
                            self.event("plan", task.plan_contract.to_dict())
                        # Planner may have completed after pause, stop, or resume generation.
                        with self.control.dispatch(generation):
                            ledger.check()
                        if not candidates:
                            raise RuntimeError("No freshly grounded candidates; reobserve or replan")
                        attempt_indices["jev"] = 0
                        step = runtime.step(observation, candidates)
                        history.append(step)
                        self.update(
                            counters={
                                **ledger.public(),
                                "planner": {
                                    "calls": planner.planning_calls,
                                    "attempts": planner.planning_attempts,
                                    "latency_ms": planner.planning_wall_ms,
                                },
                                "jev": dict(policy.metrics),
                                "runtime": dict(runtime.metrics),
                            }
                        )
                        self.frame(page)
                        selected = next(c for c in candidates if c.id == step.decision.candidate_id)
                        evaluation = task.evaluate(observation, selected, step.receipt, step.verification)
                        refreshed = task.observe(history)
                        self.update(scene=refreshed.state)
                        self.event("observation", refreshed.to_dict())
                        if not step.receipt.success or not step.verification.passed:
                            raise RuntimeError(
                                "Action did not produce its independently verified expected effect"
                            )
                        if evaluation.success:
                            # Explicit exact DOM assertion supplements the generic task verifier.
                            actual = page.locator("[role=status]").inner_text()
                            if actual != f"Saved locally: {self.state['note_text']}":
                                raise RuntimeError("Exact note postcondition failed")
                            self.event(
                                "completion", {"actual": actual, "observer": "live DOM", "verified": True}
                            )
                            with self.lock:
                                if self.state["status"] != "running":
                                    continue
                                browser.close()
                                browser = None
                                self.status("completed")
                            return
                    except Exception as exc:
                        if self.control.status != "running":
                            # Queue owns terminal acknowledgement; never revive a stopped generation.
                            continue
                        self.control.pause()
                        self.update(
                            counters={
                                **ledger.public(),
                                "jev": dict(policy.metrics),
                                "planner": {
                                    "calls": planner.planning_calls,
                                    "attempts": planner.planning_attempts,
                                    "latency_ms": planner.planning_wall_ms,
                                },
                                "runtime": dict(runtime.metrics),
                            }
                        )
                        self.event("error", {"type": type(exc).__name__, "message": str(exc)[:300]})
                        self.status("paused", error=str(exc)[:300])
        except Exception as exc:
            failure = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            for client in clients:
                client.close()
            if failure is not None:
                self.status("failed", error=failure, command_pending=None)
