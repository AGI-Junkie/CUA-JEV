# CUA-JEV cloud development handoff · 2026-10-14

> Historical first checkpoint. The real backend bridge, configuration, budgets and updated evidence are delivered in [the current cloud bridge record](CLOUD_BRIDGE_2026-10-14.md). Simulation-only UI statements below describe the earlier checkpoint.

## Repository and environment

Work was performed only in `/workspace/CUA-JEV`, in the selected cloud execution environment. `git fetch origin main` verified the remote main at `f22e41e24f8eaca09888862aa115c0d28ee7eb91`; the initial local `work` branch was clean at that commit. Development branch: `feat/persistent-jev-workbench`. No push, PR, merge or deployment was performed. The user's Windows checkout was not accessed.

Environment: Linux x86_64, Python 3.12.14, Chromium 154.0.8037.57, existing virtual environment with pytest/Ruff/Playwright/Pillow/FastAPI. No project AGENTS.md or project-owned `.agents/skills` was found; dependency-package instructions were not treated as project instructions. Baseline: 143 passed, 3 skipped, Ruff clean.

The Library skill's supported materialization and consumer-side helper were used for the supplied Library ID, with one bounded retry. Both byte transfers failed (`download failed`). No readable local DOCX was created, so neither its ten-page version nor pages 5–7 were verified. Implementation used the complete delegation requirements. No download address was guessed and no secret or private screenshot was read or sent.

## Completion matrix

| Requirement | Delivered evidence | Status / limit |
|---|---|---|
| Persistent PlanContract | `desktop_contracts.py`, atomic local JSON checkpoint (POSIX owner-only permissions), schema validation, progress, resume with fresh observation | Implemented for one desktop subgoal containing reusable actions; hierarchical multi-subgoal task orchestration remains open |
| Current SceneState | fingerprint, timestamp, selected HWND and OS evidence when available | Linux browser adapter explicitly marks physical window evidence unavailable |
| Fresh compiler | semantic name/type/automation identity; rebind after geometry/ref changes; ambiguity gives no binding | Reused by OpenDesktopTask and existing compile_option path; no saved coordinates in contract |
| Jev normal choice | existing JevPolicy, strict runtime mode, trusted privacy wrapper, JevDecision scene/candidate/request binding | Rule transport fallback disabled; explicit legacy rule benchmarks remain labeled baseline |
| Input and independent effect | ActionReceipt separate from Verification; click requires declared effect transitioning false→true; fill reads the matching semantic field | No generic UIA/pixel change can prove click success |
| Desktop capability paths | click/fill plus opt-in bounded press/scroll with explicit expected effects | Drag unsupported; namespaced computer host retains click/fill grammar |
| Execution safety | frame age, HWND/PID/focus/DPI/geometry lease, current target bounds, recheck after pointer move, no focus stealing before dispatch | Unit tests use simulated OS evidence; real Windows behavior unverified |
| Pause/stop/timeout | generation invalidation, no late dispatch after pause/stop/resume, request deadline, no overlapping timed-out policy request | Cannot undo inputs already emitted; pause acknowledgement waits for current executor to return; currently backend API object, not live generic UI controls |
| Model telemetry | logical calls, transport attempts, elapsed time, failures; planner/vision counters | Simulated latency is not model latency or evidence of acceleration |
| UI/HCI | workbench/settings/history, scope, centered synthetic screenshot, plan/action phases, takeover/reconfirm, faults and recovery | Reviewable visual proposal, not user-approved aesthetics; explicitly fixed-task simulation, no generic live task bridge |
| Cloud acceptance | real Chromium inputs and captures; two layouts each change geometry/DOM ordering between steps | DOM observer substitutes for UIA; planner/Jev responses simulated, VLM not exercised |
| Privacy/settings | prototype password inputs clear without storage/network; read-only backend history | Real key store, role configuration save and budget enforcement remain implementation work |

## Run and inspect

From repository root (existing cloud environment):

```bash
.venv/bin/python -m cua_jev.ui.app --port 8768
# Open http://127.0.0.1:8768/workbench
# In another terminal, while the server is running:
.venv/bin/python scripts/check_workbench.py

.venv/bin/python scripts/run_controlled_acceptance.py --output artifacts/controlled-acceptance
.venv/bin/python -m pytest -rs
.venv/bin/python -m ruff check .
```

For a fresh Linux environment install `.[dev,ui,browser]` and a supported Chromium/Playwright browser first. Browser tests skip only if browser prerequisites are absent. The controlled harness blocks external page requests and uses a local fixture. It does not need credentials. Its known JSONL outputs are replaced on each run to keep traces coherent.

UI evidence: `docs/evidence/workbench-{workspace,completed,recovery,settings,history,mobile}.png` and `workbench-checks.json`. The visual direction and full flow are documented in `docs/ui-design.md`. This UI runs no operating-system inputs or model calls. It must be connected to scope-bound backend start/pause/resume/stop and explicit acknowledgements before presenting live control.

Real browser evidence: `artifacts/controlled-acceptance/evidence.json`, before/after PNGs, each layout's JSONL and local plan fixture JSON. Each layout: 1 simulated planning call, 2 simulated Jev HTTP choices, 2 verified actual browser actions, 0 external page requests, 0 paid API calls. Exact measured times are in evidence JSON; no speedup is claimed. `jev_decision` events bind scene, offered-set hash, contract and API request to receipt decision IDs.

## Verification and review

Final development test run: **170 passed, 3 skipped**, Ruff and `git diff --check` clean. One existing Starlette/httpx deprecation warning remains. Skips and exact terminal evidence are retained in `docs/evidence/cloud-tests.txt`.

UI: **15 actual Chromium interaction checks**, no browser page errors; covers both simulated layouts, scope required/revocation, pause/stop late callbacks, takeover, focus/stale/timeout/no-effect recovery, key clearing, read-only history and narrow viewport overflow. These checks establish prototype behavior only.

Independent review found and drove fixes for stale cancellation, wrapper compatibility, timeout overlap, plan text leakage, wrong-field verification, persisted grammar validation, navigation side effects, missing cross-surface expectations and scope revocation. Independent reviewer ran a full suite (165 passed, 3 skipped at that point) and a later focused integration suite (51 passed); final additions are covered by the final development run. No unresolved severe blocker was found in the bounded backend slice. The remaining live UI bridge is a real product gap, not an external dependency.

## Windows / authorized real-API acceptance checklist

These are remaining verification tasks, not claims of tested capability:

1. In an isolated Windows test checkout, install `.[dev,windows,ui,browser-vision]`; open only the no-account local fixture and select exactly one non-sensitive window. Do not alter or clean the user's existing uncommitted checkout.
2. Supply authorized Jev and planner/VLM credentials through process environment or a future secure key UI. Confirm model role/name/endpoints and explicit screenshot-upload consent. No real API calls were authorized or performed in this cloud task.
3. Run `open-desktop` with Jev (default), explicit `--allow-text-input` / `--allow-button-actions`; add `--allow-navigation-input` only for the navigation test. Add `--contract-path` for private local checkpointing, then `--resume-contract` in a fresh run to test rebind. Use a separately selected `--vision-model` and `--allow-screenshot-upload` for VLM tests. `--trace` and checkpoints can contain task text; use local synthetic data.
4. Verify live screenshot→VLM→planner→fresh candidates→Jev choice→physical input→independent expected result in both layouts; record role calls, attempts and elapsed time. Confirm no rule/other-model action fallback when Jev is disconnected or times out.
5. While a model request is pending, pause, stop, then resume separately; prove old result cannot dispatch. During input, test foreground takeover and report input already emitted separately from later blocked inputs.
6. Move/resize the selected window, change DPI/display, replace the HWND/process, obscure/move the target, alter a small visual target and expire the frame. Each mismatch must reject/reobserve without blind clicking. Current screenshot comparison is conservative and not a proof of arbitrary visual target stability.
7. Test no-effect/unrelated-change click, pre-existing success text, duplicate names, missing targets, readonly/disabled targets, wrong-field equal text, bounded keyboard/scroll and unsupported drag. Verify reobserve/replan/human correction paths and completion evidence.
8. Validate Windows directory ACL protection for checkpoint/trace files; POSIX owner-only checkpoint permissions are covered by a cloud regression test. Integrate and test the live generic workbench API, secure settings, enforced budgets and acknowledgement semantics before calling this a functional end-to-end desktop product. Windows absence does not block that remaining cloud implementation work.

Review state: suitable for code/design review and continued development on this branch; **not release-ready as a complete general desktop product**. Payment, external submissions and real-money gambling remain outside the acceptance scope.
