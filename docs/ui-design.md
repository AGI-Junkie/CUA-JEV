# Workbench UI proposal — 2026-10-14

Status: implemented interactive **simulation**, visual direction proposed for review. Not user-approved aesthetics. Entry: `/workbench` (also `/static/workbench.html`) served by `cua-jev-ui`. Run `.venv/bin/python -m cua_jev.ui.app --port 8768`, then open `http://127.0.0.1:8768/workbench`. The existing research showcase remains available.

## Internal design review before implementation

The research homepage is useful evidence but does not provide a task-oriented product. The proposed workbench uses a warm neutral canvas, a centered screenshot, a persistent task/scope column and a visible plan/action column. Navy actions and amber scope warnings distinguish operation from evidence. Settings and history use the same navigation. All three action phases (proposed, input receipt, independent verification) remain separate; an input receipt never means task success.

Review decisions: scope confirmation is mandatory before simulation starts; setup is reachable before task creation; keys have password inputs, no persistence or network path; stop invalidates the pending timer; takeover requires a correction and reconfirmation; failures freeze the loop and provide reobserve/replan actions; two layout replays exercise the same semantic task. Narrow screens preserve source order and scroll, without hiding emergency stop. Keyboard focus and status announcements are required. No drag control is offered because reliable drag support is not established.

## Implemented interaction contract

1. First configuration: model roles/address/name, budget and privacy; save only acknowledges this browser-session proposal. Keys are cleared. Live configuration is explicitly unavailable.
2. Create task/context; choose a controlled local demo layout. Confirm the previewed window/scope.
3. Start a replay: persistent semantic plan → fresh candidate from the current replay scene → **simulated Jev decision** → simulated input receipt → expected-effect verification. No paid API or operating-system input.
4. Pause or emergency stop cancels pending callbacks. Takeover records the user's correction, invalidates confirmation and returns to scope review. Resume needs fresh confirmation.
5. Inject focus loss, stale frame, timeout or no-effect to review recovery. Reobserve resumes from a new replay frame; replan resets the semantic subgoal sequence.
6. Completion requires all independent expected-effect checks. History contains factual simulation events, local sequence/time and evidence status. Existing backend history is separately fetched read-only on request.

## Scope and limitations

The centered SVG is a synthetic screenshot replay, not a live capture or a real browser execution. Two layouts are engineering acceptance scenarios, not proof of arbitrary apps/games. Foreground input can affect the selected desktop; no background isolation is claimed. Real-money gambling, payments and external submission are outside scope. No settings, tasks, screenshots or keys are sent by the prototype. Keys are never stored, logged or echoed. Refresh clears all prototype state. External backend run IDs are rendered as text.

## Backend integration requirements

A production API must expose current `SceneState` including frame timestamp, window identity/focus, geometry/DPI and current fresh candidates; a persistent `PlanContract`; `JevDecision`; execution receipts; independent expected-effect verification; per-role call counts/latency; session generation cancellation; and scope-bound start/pause/resume/stop/reobserve/replan operations. Pause/stop acknowledgement must precede a UI claim that real dispatch stopped. Backend key storage needs an OS secret store or process-only input; this UI intentionally does not invent a key-save endpoint. Existing `/api/runs` supports task-specific runs and stop, not this generic contract. The live-history button is read-only and labels its existing source.

## Review evidence

See `docs/evidence/workbench-*.png` for actual browser-rendered prototype screenshots when generated. Browser checks cover configuration, required scope, both replay layouts, verified completion, pause/late callbacks, takeover/reconfirmation, stop and injected failure recovery. These are UI simulation checks; model and Windows validation are separate.

Validated: 15 browser checks passed with no page errors; existing `tests/test_ui.py`: 8 passed. Independent review identified scope revocation and focus reconfirmation gaps; both fixed and covered by browser checks.

Reproduce browser evidence: start the UI as above, then run `.venv/bin/python scripts/check_workbench.py` (Playwright plus system Chromium, or Playwright-installed Chromium). `CUA_JEV_UI_URL` overrides the localhost base URL. These checks intentionally wait for delayed callbacks to prove pause, stop and scope revocation prevent replay advancement.
