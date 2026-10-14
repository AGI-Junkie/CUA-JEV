# Controlled Chromium acceptance

Run from the repository with the browser dependency group installed and Chromium available:

```sh
python scripts/run_controlled_acceptance.py --output artifacts/controlled-acceptance
pytest tests/test_controlled_acceptance.py
```

The test executes **real headless Chromium mouse and keyboard inputs** against a local HTML fixture. No account, payment, external submission, or external network is used. The fixture loads through Playwright `set_content` because the cloud browser disallows `file://` navigation.

The planner and Jev HTTP server responses are **simulated test fixtures**. Every ordinary action still traverses the real `JevPolicy` adapter, `AgentRuntime`, `ActionGuard`, the shared `OpenDesktopTask` compilation path and task verifier; `httpx.MockTransport` supplies the server responses. This does not validate real Jev intelligence, real planner reasoning, screenshot understanding, or Windows UIA. DOM semantic wrappers stand in for the UIA observation interface; screenshot PNGs are evidence, not model inputs. No secrets or paid calls are used.

Two initial arrangements (`columns` and `stacked`) each perform a fill and save. Between actions, the harness changes layout and DOM order, so control references and button geometry change. Acceptance asserts one persistent plan invocation, two verified actions, changed button bounds, and an independently checked exact final DOM receipt. Mouse coordinates are measured afresh by the shared desktop executor, never embedded in fixture-specific control code.

`evidence.json` records separate logical Jev calls and transport attempts, per-call elapsed time, planner calls/time, selected candidates, observed button bounds and browser inputs. JSONL traces preserve observations, offered candidates, decisions, guard checks, execution receipts and verification evidence. Timings describe this simulated-model harness and make no production acceleration claim.

Remaining validation includes real Windows selected-window focus/DPI/movement, actual API model credentials and quality, screenshot visual grounding, and additional tasks/applications. This controlled acceptance does not establish arbitrary application or game support.
