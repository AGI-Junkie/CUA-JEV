"""Real local workbench endpoints, controlled HTTP model fixtures only."""
from __future__ import annotations

import importlib.util
import json
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from cua_jev.ui.app import create_app

ROOT = Path(__file__).resolve().parents[1]
BROWSER = bool(shutil.which("chromium") and importlib.util.find_spec("playwright"))


def wait(client, session_id, expected, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/workbench/sessions/{session_id}").json()
        if state["status"] in expected and not state.get("command_pending"):
            return state
        assert state["status"] != "failed", state
        time.sleep(0.025)
    raise AssertionError(f"Expected {expected}; last state {state}")


def test_configuration_local_csrf_and_secret_boundary(tmp_path):
    with TestClient(create_app(root=ROOT, data=tmp_path)) as client:
        bootstrap = client.get("/api/workbench/bootstrap").json()
        assert bootstrap["capabilities"]["live_api_enabled"] is False
        assert client.put("/api/workbench/config", json={}).status_code == 403
        headers = {"x-cua-jev-csrf": bootstrap["csrf"]}
        key = "test-input-key-never-saved"
        response = client.put("/api/workbench/config", headers=headers, json={
            "roles": {"jev": {"name": "configured-unverified", "api_key": key}},
            "limits": {"max_steps": 3},
        })
        assert response.status_code == 200
        assert key not in response.text
        config = response.json()
        assert config["roles"]["jev"]["key_configured"] is True
        assert config["roles"]["jev"]["verified"] is False
        assert config["cost"]["ceiling_enforced"] is False
        assert key not in (tmp_path / "workbench/configuration.json").read_text()
        assert client.get("/api/workbench/sessions/missing").status_code == 404
        assert client.post("/api/workbench/sessions", headers=headers, json=[]).status_code == 400
        assert client.put("/api/workbench/config", headers=headers, json={"mode": "live"}).status_code == 400
    # Restart never restores a credential.
    with TestClient(create_app(root=ROOT, data=tmp_path)) as client:
        assert client.get("/api/workbench/config").json()["roles"]["jev"]["key_configured"] is False


@pytest.mark.skipif(not BROWSER, reason="requires installed local Chromium and Playwright")
def test_real_workbench_http_session_frames_events_and_stream(tmp_path):
    with TestClient(create_app(root=ROOT, data=tmp_path)) as client:
        headers = {"x-cua-jev-csrf": client.get("/api/workbench/bootstrap").json()["csrf"]}
        body = {"task": "Save my acceptance note", "context": "Local no-account task",
                "note_text": "Actual API bridge note", "layout": "stacked"}
        created = client.post("/api/workbench/sessions", headers=headers, json=body)
        assert created.status_code == 201
        sid = created.json()["id"]
        base = f"/api/workbench/sessions/{sid}"
        state = wait(client, sid, {"scope_ready"})
        assert client.post(base + "/start", headers=headers, json={}).status_code == 409
        assert client.post("/api/workbench/sessions", headers=headers, json=body).status_code == 409
        preview = client.get(state["frame_url"])
        assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
        assert client.post(base + "/confirm", headers=headers, json={}).status_code == 200
        wait(client, sid, {"ready"})
        assert client.post(base + "/start", headers=headers, json={}).status_code == 200
        completed = wait(client, sid, {"completed"})
        facts = client.get(base + "/events").json()["events"]
        kinds = [event["type"] for event in facts]
        assert kinds.count("receipt") == 2
        assert kinds.count("verification") == 2
        assert "jev_decision" in kinds
        assert completed["plan"]["contract_id"]
        assert (tmp_path / "workbench/sessions" / sid / "plan.json").is_file()
        stream = client.get(base + "/stream?once=true", headers={"last-event-id": str(facts[-3]["seq"])})
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "event: snapshot" in stream.text
        streamed = [json.loads(line[6:]) for line in stream.text.splitlines()
                    if line.startswith("data: ") and '"seq"' in line]
        assert [e["seq"] for e in streamed] == [e["seq"] for e in facts[-2:]]
        assert client.get(base + "/stream", headers={"last-event-id": "bad"}).status_code == 400
        assert client.get(base + "/events?after=-1").status_code == 400
        assert client.post(base + "/unknown", headers=headers, json={}).status_code == 404
        assert completed["capabilities"]["external_network"] is False
    with TestClient(create_app(root=ROOT, data=tmp_path)) as client:
        history = client.get("/api/workbench/sessions").json()
        assert history[0]["id"] == sid and history[0]["status"] == "completed"
        assert client.get(base + "/frame").content.startswith(b"\x89PNG")


def test_sse_drains_completion_that_arrives_between_batch_and_status(monkeypatch, tmp_path):
    class RacingSessions:
        """Deterministic transport race fixture; real browser flow is tested above."""
        def __init__(self, *args, **kwargs):
            self.status = "running"
            self.facts = [{"seq": 1, "type": "observation", "at": 0, "data": {}}]
            self.first = True

        def get(self, sid):
            return {"id": sid, "status": self.status}

        def events(self, sid, after):
            result = {"events": [e for e in self.facts if e["seq"] > after], "last_seq": len(self.facts)}
            if self.first:
                self.first = False
                self.facts.extend([
                    {"seq": 2, "type": "completion", "at": 1, "data": {"verified": True}},
                    {"seq": 3, "type": "lifecycle", "at": 1, "data": {"status": "completed"}},
                ])
                self.status = "completed"
            return result

        def close(self):
            pass

    monkeypatch.setattr("cua_jev.ui.workbench_api.ControlledSessionManager", RacingSessions)
    with TestClient(create_app(root=ROOT, data=tmp_path)) as client:
        response = client.get("/api/workbench/sessions/race/stream")
        assert response.status_code == 200
        facts = [json.loads(line[6:]) for line in response.text.splitlines()
                 if line.startswith("data: ") and '"seq"' in line]
        assert [e["seq"] for e in facts] == [1, 2, 3]
        assert response.text.index('"completion"') < response.text.index("event: end")


@pytest.mark.skipif(not BROWSER, reason="requires installed local Chromium and Playwright")
def test_budget_pause_stop_stream_reconnect_preserves_facts_and_counters(tmp_path):
    with TestClient(create_app(root=ROOT, data=tmp_path)) as client:
        headers = {"x-cua-jev-csrf": client.get("/api/workbench/bootstrap").json()["csrf"]}
        assert client.put("/api/workbench/config", headers=headers,
                          json={"limits": {"max_steps": 1}}).status_code == 200
        response = client.post("/api/workbench/sessions", headers=headers,
                               json={"task": "Save local budget note", "note_text": "Bounded note"})
        assert response.status_code == 201
        sid = response.json()["id"]
        base = f"/api/workbench/sessions/{sid}"
        wait(client, sid, {"scope_ready"})
        assert client.post(base + "/confirm", headers=headers, json={}).status_code == 200
        wait(client, sid, {"ready"})
        assert client.post(base + "/start", headers=headers, json={}).status_code == 200
        paused = wait(client, sid, {"paused"})
        assert "Step budget" in paused["error"]
        assert paused["counters"]["steps"] == 1
        facts = client.get(base + "/events").json()["events"]
        receipt = next(e for e in facts if e["type"] == "receipt")
        cursor = receipt["seq"]
        stream = client.get(base + "/stream?once=true", headers={"last-event-id": str(cursor)})
        streamed = [json.loads(line[6:]) for line in stream.text.splitlines()
                    if line.startswith("data: ") and '"seq"' in line]
        assert [e["seq"] for e in streamed] == [e["seq"] for e in facts if e["seq"] > cursor]
        assert client.post(base + "/resume", headers=headers, json={}).status_code == 200
        resumed = wait(client, sid, {"paused"})
        assert resumed["counters"]["steps"] == 1
        assert resumed["counters"]["model_attempts"] == paused["counters"]["model_attempts"] + 1
        assert resumed["counters"]["jev"]["calls"] == paused["counters"]["jev"]["calls"] + 1
        assert client.post(base + "/stop", headers=headers, json={}).status_code == 200
        stopped = wait(client, sid, {"stopped"})
        assert stopped["counters"]["steps"] == 1
        assert stopped["counters"]["model_attempts"] == resumed["counters"]["model_attempts"]
        all_facts = client.get(base + "/events").json()["events"]
        replay_cursor = streamed[-1]["seq"]
        replay = client.get(base + "/stream", headers={"last-event-id": str(replay_cursor)})
        replayed = [json.loads(line[6:]) for line in replay.text.splitlines()
                    if line.startswith("data: ") and '"seq"' in line]
        assert [e["seq"] for e in replayed] == [e["seq"] for e in all_facts if e["seq"] > replay_cursor]
        assert replayed[-1]["data"]["status"] == "stopped"
        assert "event: end" in replay.text
        assert sum(e["type"] == "receipt" for e in all_facts) == 1
        assert not any(e["type"] == "completion" for e in all_facts)
