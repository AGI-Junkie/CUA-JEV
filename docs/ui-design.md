# Workbench product proposal — real browser session integration

2026-10-14. Visual direction proposed for review; not a user-selected style. The earlier synthetic screenshot replay has been replaced by a real controlled Chromium session backend. No JavaScript-generated task success or model counters remain.

## Internal design review and implemented flow

The existing research homepage did not support a task-oriented product. The workbench uses a warm neutral canvas, a centered actual browser screenshot, persistent task/scope controls, and a plan/action panel. Settings and factual history share navigation. The screenshot stays central; detailed trace JSON is behind expandable history evidence.

1. Configuration: planner, Jev and vision roles expose model name/address and password input. Settings are stored through the local backend; secrets are process-memory only, not returned or written to disk. Password inputs clear immediately on submission, including failed requests. Blank preserves a key; explicit checkboxes clear it. All actual paid API execution remains disabled. Config changes apply only to newly created sessions.
2. Task/context/note/layout: create a backend session, launch real headless Chromium and preview its actual PNG. Local scope confirmation is required before the backend confirm/start commands. The current engineering task is exact local note saving; the mocked planner is a controlled test service, not general task intelligence.
3. Run: the backend supplies the persistent semantic PlanContract, fresh SceneState, actual JevPolicy API decision parsing, real mouse/keyboard receipts and independent expected-effect verification. The UI polls authoritative state and ordered facts. API request counts and model latency come from backend measurements; missing evidence stays unknown. API responses are simulated; browser execution is real.
4. Pause/stop: accepted command and completed acknowledgement are separate states. Buttons show `pausing`, `stopping` and `starting` until the worker confirms. Commands with unchanged lifecycle status wait for `command_pending` to clear. Emergency stop can supersede pending UI operations. Preview replacement waits for the old worker to stop before creating another.
5. Takeover/correction: pause is acknowledged before takeover. The user changes the note/context/layout and records a correction; the backend resets its task and invalidates scope. Reconfirmation is required before resuming. Changed form fields remain dirty across polling: start/resume/scope stay blocked until correction or a new preview, avoiding execution of an old backend task behind new form text.
6. Recovery: a backend error pauses progress, retains receipts and shows reobserve/replan controls only where the backend supports them. Budget failure is captured as a real controlled-browser error, never task completion. Scope revocation pauses an active worker and calls backend `invalidate_scope`.
7. Refresh: the UI fetches the authoritative active session, restores its task, scope state, screenshot and complete event cursor, and restores stop/control access. No secrets or task state are stored in browser localStorage.
8. Completion: only the backend's verified completion ends the task. History separates Jev decisions, input receipts, independent postconditions and exact terminal results; detailed raw evidence is available on request.

## Observers and capability boundary

Default observation uses real DOM semantics as the controlled browser adapter's UIA-shaped input. It is not VLM understanding. An optional `mock_vision` path is explicitly labelled screenshot plus simulated vision API; its controlled test service derives targets from the test fixture. No real vision model capability is claimed. Both paths use real screenshot PNGs. The UI displays the backend observer capability rather than assuming a live VLM.

Two browser layouts are engineering acceptance scenarios, not arbitrary application or game coverage. Browser input runs in cloud headless Chromium; this does not demonstrate Windows foreground input isolation. Reliable drag support is absent. Real-money gambling, payments and external submission are outside scope. Local session screenshots/events persist in the backend evidence directory; they are not uploaded to a model by this workbench. Actual Windows, API-key authorization and real provider inference remain separate validation.

## Independent review fixes

Independent review identified scope revocation, command acknowledgement, paused-field dirty state, replacing active previews, refresh control recovery and configuration snapshot messaging gaps. Fixes are implemented; the browser checks exercise real backend transitions, reload, delayed polling and correction. Existing research pages remain intact and link to `/workbench`.

## Run and evidence

Start `.venv/bin/python -m cua_jev.ui.app --port 8768`, then open `http://127.0.0.1:8768/workbench`.

Reproduce browser integration evidence: `.venv/bin/python scripts/check_workbench.py`. Requires Playwright plus system Chromium or a Playwright-installed Chromium. `CUA_JEV_UI_URL` overrides the localhost base URL. The script creates only local test notes, verifies exact saved text in both layouts, exercises settings/key clearing, a backend budget failure, scope revocation, takeover/correction, refresh restoration, stop and responsive rendering. It makes no paid API calls. Lower-level session tests separately exercise focus loss, stale frames, timeout, no effect and late results.

Evidence: `docs/evidence/workbench-workspace.png`, `workbench-completed-columns.png`, `workbench-completed-stacked.png`, `workbench-completed-vision.png`, `workbench-recovery.png`, `workbench-takeover.png`, `workbench-settings.png`, `workbench-history.png`, `workbench-mobile.png`; machine-readable results in `workbench-checks.json`. These are actual browser-rendered product screenshots. The centered scene is an actual backend Chromium capture, not an SVG or replay illustration.

Latest evidence: 17 browser integration checks passed with no page errors. The optional vision flow measured nonzero vision API calls and a real `desktop.visual_click` receipt. Existing UI tests: 8 passed; script Ruff and JavaScript syntax checks pass. No real provider/Windows validation is implied.
