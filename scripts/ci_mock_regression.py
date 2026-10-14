"""Run isolated, real-browser UI and cross-layout flows with local model mocks.

This runner never enables live model execution. It starts its own localhost
server/data directory and cleans up only that process, including on failures.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(output: Path):
    if not shutil.which("chromium"):
        raise RuntimeError("Required Chromium is absent; refusing a skipped mock-dataflow run")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    runtime_data = Path(tempfile.mkdtemp(prefix="server-data-", dir=output))
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    # Provider credentials are deliberately absent from the child environment.
    env = os.environ.copy()
    for name in ("TYPESAFE_API_KEY", "CUA_JEV_MODEL_API_KEY", "CUA_JEV_PLANNER_API_KEY"):
        env.pop(name, None)
    env["CUA_JEV_UI_URL"] = url
    env["CUA_JEV_UI_EVIDENCE_DIR"] = str(output / "ui")
    with (output / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "cua_jev.ui.app", "--port", str(port),
                                    "--data", str(runtime_data)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(trust_env=False, timeout=2) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Isolated UI server exited; inspect server.log")
                    try:
                        response = client.get(url + "/api/workbench/bootstrap")
                        response.raise_for_status()
                        state = response.json()
                        if state["capabilities"]["live_api_enabled"] or state["sessions"]:
                            raise RuntimeError("Expected a fresh, mock-only server")
                        break
                    except (httpx.HTTPError, ValueError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Isolated UI server did not become ready") from None
                        time.sleep(0.1)
            subprocess.run([sys.executable, str(ROOT / "scripts/check_workbench.py")],
                           cwd=ROOT, env=env, check=True, timeout=180)
            subprocess.run([sys.executable, str(ROOT / "scripts/run_controlled_acceptance.py"),
                            "--output", str(output / "cross-layout")],
                           cwd=ROOT, env=env, check=True, timeout=60)
            evidence = json.loads((output / "cross-layout/evidence.json").read_text())
            if any(r["paid_api_calls"] or r["external_network_requests"] for r in evidence["layouts"]):
                raise RuntimeError("Mock-only execution contract violated")
            (output / "summary.json").write_text(json.dumps({
                "mode": "local HTTP model mocks, real Chromium input", "paid_api_calls": 0,
                "ui_checks": json.loads((output / "ui/workbench-checks.json").read_text()),
                "cross_layout": evidence,
            }, indent=2), encoding="utf-8")
        finally:
            process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/ci/data-flow")
    run(parser.parse_args().output)
