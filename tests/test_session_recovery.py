"""Crash-tail recovery and pre-persistence input validation regressions."""

import json
import threading
from pathlib import Path

import pytest

from cua_jev.ui.sessions import ControlledSessionManager, _Session

ROOT = Path(__file__).resolve().parents[1]


def stopped_history(directory):
    manager = ControlledSessionManager(ROOT, directory)
    session = _Session(manager, {"task": "Save local note", "note_text": "Recovery note"}, None)
    session.event("receipt", {"success": False, "input_delivered": False})
    session.status("stopped")
    return session, (session.directory / "events.jsonl").read_bytes()


@pytest.mark.parametrize("tail", [b'{"seq":4,"type":', b'{"seq":4,"data":{"text":"\xe4\xb8'])
def test_restart_replays_durable_prefix_after_torn_json_or_utf8_tail(tmp_path, tail):
    session, prefix = stopped_history(tmp_path)
    path = session.directory / "events.jsonl"
    path.write_bytes(prefix + tail)
    recovered = ControlledSessionManager(ROOT, tmp_path)
    assert recovered.get(session.id)["status"] == "stopped"
    assert recovered.events(session.id) == {"events": session.events, "last_seq": 3}
    assert recovered.events(session.id, after=1) == {"events": session.events[1:], "last_seq": 3}
    assert recovered.events(session.id, after=3) == {"events": [], "last_seq": 3}
    assert path.read_bytes() == prefix + tail

    # A repaired record keeps its actual durable sequence, without renumbering replay.
    repair = {"seq": 4, "type": "recovery_note", "at": 0, "data": {"synthetic": True}}
    path.write_bytes(prefix + json.dumps(repair).encode("utf-8") + b"\n")
    assert recovered.events(session.id, after=3) == {"events": [repair], "last_seq": 4}
    assert recovered.events(session.id, after=4) == {"events": [], "last_seq": 4}


def test_history_events_and_sse_keep_prefix_after_torn_tail(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from cua_jev.ui.app import create_app

    session, prefix = stopped_history(tmp_path / "workbench" / "sessions")
    (session.directory / "events.jsonl").write_bytes(prefix + b'{"seq":4')
    with TestClient(create_app(root=ROOT, data=tmp_path)) as client:
        base = f"/api/workbench/sessions/{session.id}"
        history = client.get("/api/workbench/sessions").json()
        assert history[0]["id"] == session.id and history[0]["status"] == "stopped"
        response = client.get(base + "/events?after=1")
        assert response.status_code == 200
        assert response.json() == {"events": session.events[1:], "last_seq": 3}
        stream = client.get(base + "/stream", headers={"last-event-id": "1"})
        assert stream.status_code == 200
        facts = [json.loads(line[6:]) for line in stream.text.splitlines()
                 if line.startswith("data: ") and '"seq"' in line]
        assert facts == session.events[1:]
        assert "event: end" in stream.text


@pytest.mark.parametrize(
    "corruption",
    [
        b'{"seq":4\n',  # A terminated bad record is not a crash-truncated tail.
        b'{"seq":4\n{"seq":5,"type":"later","at":0,"data":{}}\n',
        b'{"seq":3,"type":"duplicate","at":0,"data":{}}\n',
        b'{"seq":5,"type":"gap","at":0,"data":{}}\n',
        b'{"seq":true,"type":"boolean","at":0,"data":{}}',
        b'[]',
        b'{"seq":4}',  # Valid JSON must still describe a complete fact.
        b'{"seq":4,"type":4,"at":0,"data":{}}',
        b'{"seq":4,"type":"","at":0,"data":{}}',
        b'{"seq":4,"type":"bad_time","at":true,"data":{}}',
        b'{"seq":4,"type":"bad_time","at":NaN,"data":{}}',
        b'{"seq":4,"type":"bad_payload","at":0,"data":[]}',
    ],
)
def test_history_never_swallows_midstream_or_sequence_corruption(tmp_path, corruption):
    session, prefix = stopped_history(tmp_path)
    path = session.directory / "events.jsonl"
    path.write_bytes(prefix + corruption)
    recovered = ControlledSessionManager(ROOT, tmp_path)
    with pytest.raises(ValueError):
        recovered.events(session.id)
    assert path.read_bytes() == prefix + corruption


@pytest.mark.parametrize("field", ["task", "context"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_create_rejects_surrogates_before_creating_or_starting_session(
    monkeypatch, tmp_path, field, surrogate
):
    started = []
    monkeypatch.setattr(_Session, "run", lambda self: started.append(self.id))
    manager = ControlledSessionManager(ROOT, tmp_path)
    payload = {"task": "Save local note", "context": "Synthetic context", "note_text": "Safe note"}
    payload[field] = "before" + surrogate + "after"
    try:
        with pytest.raises(ValueError, match=field + ".*UTF-8"):
            manager.create(payload)
        assert not manager.list()
        assert not list(tmp_path.iterdir())
        assert not started
    finally:
        manager._sessions.clear()


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_correction_rejects_surrogate_context_without_scope_or_queue_change(tmp_path, surrogate):
    manager = ControlledSessionManager(ROOT, tmp_path)
    session = _Session(manager, {"task": "Save local note", "note_text": "Original note"}, None)
    session.update(status="paused", scope_confirmed=True)
    before = session.public()
    before_disk = (session.directory / "session.json").read_bytes()
    with pytest.raises(ValueError, match="context.*UTF-8"):
        session.command("correct", {"context": "before" + surrogate + "after"})
    assert session.public() == before
    assert (session.directory / "session.json").read_bytes() == before_disk
    assert session.commands.empty()


@pytest.mark.parametrize("default_encoding", [None, "cp1252"])
def test_valid_unicode_context_and_task_persist_and_replay(monkeypatch, tmp_path, default_encoding):
    if default_encoding is not None:
        original_read = Path.read_text

        def read_with_legacy_default(path, encoding=None, errors=None):
            return original_read(path, encoding=encoding or default_encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", read_with_legacy_default)
    monkeypatch.setattr(_Session, "run", lambda self: None)
    manager = ControlledSessionManager(ROOT, tmp_path)
    text = "中文 😀 e\u0301 separator\u2028in context"
    state = manager.create({"task": text, "context": text, "note_text": "Valid 😀 note"})
    session = manager._sessions[state["id"]]
    session.thread.join(timeout=1)
    session.event("model_request", {"goal": text})
    session.status("stopped")
    recovered = ControlledSessionManager(ROOT, tmp_path)
    assert recovered.get(session.id)["task"] == text
    assert recovered.get(session.id)["context"] == text
    assert recovered.events(session.id)["events"] == session.events


@pytest.mark.parametrize("field", ["layout", "observe_mode"])
@pytest.mark.parametrize("invalid", [[], {}, 0])
def test_create_rejects_nonstrings_for_enum_fields(tmp_path, field, invalid):
    manager = ControlledSessionManager(ROOT, tmp_path)
    with pytest.raises(ValueError, match="layout|observation"):
        manager.create({"task": "Save local note", "note_text": "Safe note", field: invalid})
    assert not manager.list()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("invalid", [[], {}, 0])
def test_correction_rejects_nonstring_layout_without_scope_or_queue_change(tmp_path, invalid):
    manager = ControlledSessionManager(ROOT, tmp_path)
    session = _Session(manager, {"task": "Save local note", "note_text": "Original note"}, None)
    session.update(status="paused", scope_confirmed=True)
    before = session.public()
    with pytest.raises(ValueError, match="layout"):
        session.command("correct", {"layout": invalid})
    assert session.public() == before
    assert session.commands.empty()


@pytest.mark.parametrize(
    "invalid",
    [{"task": "bad\ud800"}, {"context": "bad\udfff"}, {"layout": []}, {"observe_mode": {}}],
)
def test_api_rejects_invalid_creation_fields_before_worker_or_disk_change(monkeypatch, tmp_path, invalid):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from cua_jev.ui.app import create_app

    monkeypatch.setattr(_Session, "run", lambda self: None)
    with TestClient(create_app(root=ROOT, data=tmp_path), raise_server_exceptions=False) as client:
        manager = client.app.state.workbench_sessions
        headers = {"x-cua-jev-csrf": client.get("/api/workbench/bootstrap").json()["csrf"],
                   "content-type": "application/json"}
        payload = {"task": "Save local note", "note_text": "Safe note", **invalid}
        try:
            response = client.post("/api/workbench/sessions", headers=headers,
                                   content=json.dumps(payload).encode("utf-8"))
            assert response.status_code == 400
            assert not manager.list()
            assert not list(manager.directory.iterdir())
        finally:
            for session in manager._sessions.values():
                session.thread.join(timeout=1)
            manager._sessions.clear()


@pytest.mark.parametrize("invalid", [{"context": "bad\ud800"}, {"layout": []}])
def test_api_correction_rejects_invalid_fields_before_invalidating_scope(tmp_path, invalid):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from cua_jev.ui.app import create_app

    with TestClient(create_app(root=ROOT, data=tmp_path), raise_server_exceptions=False) as client:
        manager = client.app.state.workbench_sessions
        session = _Session(manager, {"task": "Save local note", "note_text": "Original note"}, None)
        session.update(status="paused", scope_confirmed=True)
        manager._sessions[session.id] = session
        before = session.public()
        headers = {"x-cua-jev-csrf": client.get("/api/workbench/bootstrap").json()["csrf"],
                   "content-type": "application/json"}
        try:
            response = client.post(f"/api/workbench/sessions/{session.id}/correct", headers=headers,
                                   content=json.dumps(invalid).encode("utf-8"))
            assert response.status_code == 409
            assert session.public() == before
            assert session.commands.empty()
        finally:
            manager._sessions.clear()


def test_shutdown_handles_worker_terminalizing_after_status_snapshot(monkeypatch, tmp_path):
    manager = ControlledSessionManager(ROOT, tmp_path)
    session = _Session(manager, {"task": "Save local note", "note_text": "Shutdown note"}, None)
    session.update(status="running")
    manager._sessions[session.id] = session
    complete, finished = threading.Event(), threading.Event()

    def worker_failure():
        assert complete.wait(timeout=2)
        session.status("failed", error="Synthetic shutdown race")
        finished.set()

    session.thread = threading.Thread(target=worker_failure)
    session.thread.start()
    original_public = session.public

    def snapshot_then_allow_completion():
        snapshot = original_public()
        complete.set()
        # An atomic shutdown holds the session lock here, so this worker cannot
        # terminalize until stop is queued. The previous separate check lets it.
        finished.wait(timeout=0.2)
        return snapshot

    monkeypatch.setattr(session, "public", snapshot_then_allow_completion)
    try:
        manager.close()
        assert not session.thread.is_alive()
        assert original_public()["status"] == "failed"
    finally:
        complete.set()
        session.thread.join(timeout=2)
