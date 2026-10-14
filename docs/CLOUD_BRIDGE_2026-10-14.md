# CUA-JEV cloud bridge delivery · 2026-10-14

This records the de0b1d5 delivery checkpoint. See [CI and authorized PR publishing](CLOUD_CI_2026-10-14.md) for the subsequent push/CI status. The earlier `CLOUD_HANDOFF_2026-10-14.md` describes the first checkpoint and is historical. Development remains local on `feat/persistent-jev-workbench`; at that checkpoint no push, PR, merge or deployment had been performed.

## What now runs

The workbench is connected to a real backend session manager. Its status, screenshots, plan, choices, receipts, verification, counters and history come from backend facts. JavaScript no longer manufactures task advancement or verification with replay timers.

Within the explicitly selected **local note-save lab**, the user supplies task/context, exact note content and one of two layouts. The backend creates a Chromium viewport, captures a real PNG, waits for scope confirmation, and runs OpenDesktopTask → persistent PlanContract → fresh semantic binding → JevPolicy → guarded real browser mouse/keyboard → independent exact result. The browser and all Playwright objects stay on one worker thread. This is browser input, not a Windows desktop or an isolated background replacement for foreground OS input.

Only external model responses are replaced with local HTTP fixtures. ChatModelPlanner and JevPolicy still perform their normal serialization/parsing/choice validation. Configured role model names and endpoint paths reach these adapters, but explicit MockTransport always intercepts requests. Supplying keys or endpoints cannot enable live execution. No paid API calls or private screenshot uploads were made.

Optional `observe_mode=mock_vision` also sends an actual JPEG through the vision API adapter to a local fixture, returns fresh normalized targets, and executes a Jev-selected `desktop.visual_click`. The fixture derives answers from current DOM semantics: **this tests the screenshot/adapter/action contract, not real VLM perception**. Preview, correction preview and paused reobservation do not call models. Vision starts only after scope confirmation and task start. Image bytes and keys are absent from fact traces; vision metadata contains digest, dimensions and call counts.

## Completed matrix

| Cloud scope | Result and evidence |
|---|---|
| Workbench → task loop → screenshot/evidence | Implemented `/api/workbench`; real PNG endpoint, ordered factual events, reconnectable SSE, persisted local session/plan/evidence |
| Scope/lifecycle | Preview → confirm → start → pause acknowledgement → takeover/correct → reconfirm → resume; stop acknowledgement waits for browser cleanup |
| Repeat starts/switches | Duplicate start and overlapping active session refused; switch waits for old stop acknowledgement; pending commands serialized |
| Recovery | Fresh observation/replan, model timeout, no-effect and lost-focus paths; old generation cannot dispatch; outstanding model request gate survives correction |
| UI consistency | Reload restores authoritative active session; edited forms stay dirty across polls; unsupported task/observer changes require a new session instead of executing old values |
| Configuration | Planner/vision/Jev role name and endpoint, process-only replace/clear keys; nonsecret JSON atomically persisted with POSIX owner-only permissions; snapshot per session |
| Hard boundaries | Steps, wall deadline, model transport reservations and retry bounds; budget checked immediately before executor and each mouse/keyboard emission |
| Cost | Counts and elapsed time measured; optional user-supplied per-attempt prices are estimates; provider dollar ceiling explicitly **not enforced**; no live billing claims |
| Observation modes | DOM lab and optional actual screenshot + simulated vision API; both clearly labeled; real VLM/Windows still unverified |
| Private local data | Session directory POSIX `0700`; checkpoint/config JSON `0600`; keys never serialized, returned or logged; Windows ACL remains unverified |

A wall deadline prevents a new model transport or input emission after exhaustion. It cannot undo already emitted input or forcibly terminate an in-flight provider/OS operation. Pause/stop similarly distinguish request, invalidated generation, and worker acknowledgement. This limitation is visible in lifecycle states; the UI does not announce a completed stop before acknowledgement.

The existing desktop observer can catch a denied/failed vision invocation and retain live UIA/DOM evidence. `counters.vision` therefore distinguishes adapter invocations, successful calls and failures, while the budget ledger counts admitted transports. A regression with a seven-attempt ceiling completes its already produced exact effect through independent DOM verification, records the final denied vision invocation as a failure and starts no extra input. This is not reported as universal visual success.

## Run

From repository root in the prepared cloud environment:

```bash
.venv/bin/python -m cua_jev.ui.app --port 8768
# http://127.0.0.1:8768/workbench

# While the server runs:
.venv/bin/python scripts/check_workbench.py

# Backend + API + real Chromium regression suite:
.venv/bin/python -m pytest -rs
.venv/bin/python -m ruff check .
```

For a fresh Linux checkout, install `.[dev,ui,browser]` and Chromium/Playwright first. Tests never download browsers. The session service requires the checkout's local `fixtures/controlled_browser/task.html`; it does not navigate arbitrary user URLs.

`--data` selects the local console data root. Workbench data is beneath its `workbench/` directory. Refresh restores a currently alive session from the server. After an actual server restart, unfinished sessions become `interrupted` history; a new scoped browser must be created rather than pretending a lost browser resumed.

## API map

All routes use the console's local-host/origin restrictions and CSRF on mutations. `GET /api/workbench/bootstrap` returns the CSRF token, public configuration and capabilities. Credentials are never returned.

- `GET/PUT /api/workbench/config`: nonsecret configuration and memory-only key updates. Blank key preserves; `clear_api_key=true` clears. Settings apply to newly created sessions.
- `GET/POST /api/workbench/sessions`: list or create controlled sessions. Create accepts `task`, `context`, `note_text` (1–120 characters), `layout=columns|stacked`, `observe_mode=dom|mock_vision`, and `mode=controlled_mock`. Arbitrary sites/windows/live execution and unknown fields are rejected.
- `GET /sessions/{id}` under the same prefix: authoritative state.
- `GET /sessions/{id}/frame`: actual PNG; unavailable preview returns 409.
- `GET /sessions/{id}/events?after=N`: ordered facts with monotonic sequence IDs.
- `GET /sessions/{id}/stream`: SSE snapshot then factual events; supports `Last-Event-ID`, heartbeat and reconnect. `once=true` gives a bounded diagnostic batch.
- `POST /sessions/{id}/{confirm,start,pause,resume,takeover,correct,reobserve,replan,invalidate_scope,stop}`: queued lifecycle operations. The response may be pending; clients must await `command_pending` cleared and acknowledged lifecycle status. Correction supports note text/context/layout; task or observer changes require a new session.

Limits: 1–100 steps, 1–600 seconds, 1–20 admitted model transports, 0–2 retries and 0.1–30 seconds for the Jev decision deadline. Bounds are validated and frozen per session. Attempt reservations include failed requests and retries. Pause/stop acknowledgements capture cancelled-call counts; `model_timing_complete=false` marks a still-outstanding API request, so incomplete latency is not presented as a complete total. Step reservation occurs immediately before execution, so rejected decisions consume no fictitious input step.

## Evidence and review

Final backend results are in `docs/evidence/cloud-bridge-tests.txt`. The full run after the final input-boundary additions reports **232 passed, 3 skipped**; skips require system Edge or Windows Excel. Ruff and `git diff --check` pass. An existing Starlette/httpx deprecation warning remains. A final cancellation-telemetry update was checked with the focused session/runtime/API regressions recorded beneath that full-run output.

`docs/evidence/workbench-checks.json`: **17 real browser UI integration checks**, no page errors. It covers both layouts, exact saved values, measured calls, genuine budget failure, scope revocation, reload, dirty-form protection, takeover/correction/reconfirmation, stop, optional screenshot/visual-click path, settings and mobile layout. Lower-level integration tests cover late planner/Jev results, queued stop/start, timeout overlap, retry-zero, no effect, focus loss and history restart.

Actual product screenshots: workspace, completed columns/stacked, completed vision, recovery, takeover, settings, history and mobile under `docs/evidence/`. The completed vision screenshot was also uploaded to Library for native presentation:

- Library ID: `libfile_7e38b8d021f88191907dbf0cca757239`
- Current file: `workbench-completed-vision.png`, version 2
- File ID: `file_0000000098b081f5abe63c787d89d0c4`
- Native download/view: `https://chatgpt.com/api/library/files/libfile_7e38b8d021f88191907dbf0cca757239/download`

Independent review drove fixes for command races, timeout gate reuse, retry bounds, cleanup-before-terminal acknowledgement, event-loop blocking, draining final SSE facts across a completion race, stale UI form execution and role endpoint validation. The final bounded scope has no unresolved severe review blocker. These tests do not establish real model quality, arbitrary-app generality or Windows behavior.

## Remaining external validation and deliberate scope limits

1. Authorized real Jev/planner/VLM credentials and explicit paid-call/screenshot consent are needed for real provider validation. The workbench deliberately rejects live mode in this no-paid-call build; existing CLI adapters remain available for separately authorized testing.
2. Windows HWND/PID/DPI/focus, foreground takeover, stale small visual targets, clipboard/input behavior and file ACLs still need Windows tests from the earlier checklist. No user workstation was accessed.
3. Arbitrary applications/games, robust drag and general hierarchical multi-subgoal planning are not established by a local two-action note-save lab. Payments, external submissions and real-money gambling remain out of scope.
4. Provider dollar ceilings cannot be guaranteed from unverified price/billing data. They remain explicitly unenforced; runtime action/time/retry/attempt limits are enforced.
5. The Library handoff report was not retried again; its initial supported materialization failed. This did not block development from the complete task requirements.

This branch is reviewable and runnable for the bounded controlled-browser product flow; it is not labeled a validated general desktop release.
