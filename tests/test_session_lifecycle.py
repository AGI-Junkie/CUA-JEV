"""Dependency failure, durable terminal facts and note-validation regressions."""

import builtins
import json
import time
import tomllib
from pathlib import Path

import pytest

from cua_jev.ui.sessions import ControlledSessionManager, _Session

ROOT = Path(__file__).resolve().parents[1]


def test_missing_playwright_is_terminal_and_releases_session_slot(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ModuleNotFoundError("No module named 'playwright'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    manager = ControlledSessionManager(ROOT, tmp_path)
    try:
        ids = []
        for _ in range(2):
            state = manager.create({"task": "Save local note", "note_text": "Dependency test"})
            ids.append(state["id"])
            deadline = time.monotonic() + 3
            while manager.get(state["id"])["status"] != "failed":
                assert time.monotonic() < deadline
                time.sleep(0.01)
            failed = manager.get(state["id"])
            assert "ModuleNotFoundError" in failed["error"]
            events = manager.events(state["id"])["events"]
            assert events[-1]["data"]["status"] == "failed"
        assert ids[0] != ids[1]
    finally:
        manager.close()


def test_ui_extra_installs_browser_runtime_and_vision_image_decoder():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["optional-dependencies"]["ui"]
    assert any(d.startswith("playwright") for d in dependencies)
    assert any(d.startswith("Pillow") for d in dependencies)


@pytest.mark.parametrize(
    "note",
    [
        "line\nbreak",
        "line\rbreak",
        "two  spaces",
        " leading",
        "trailing ",
        "a\tb",
        "a\u2028b",
        "a\u00a0b",
        "a\x00b",
        "a\ud800b",
    ],
)
def test_create_rejects_notes_the_single_line_rendered_lab_cannot_preserve(tmp_path, note):
    manager = ControlledSessionManager(ROOT, tmp_path)
    with pytest.raises(ValueError, match="single-line"):
        manager.create({"task": "Save local note", "note_text": note})
    assert not manager.list()


@pytest.mark.parametrize("note", ["line\nbreak", "two  spaces", " trailing ", "a\tb"])
def test_correction_rejects_unpreservable_note_without_invalidating_scope(tmp_path, note):
    manager = ControlledSessionManager(ROOT, tmp_path)
    session = _Session(manager, {"task": "Save local note", "note_text": "Original note"}, None)
    session.update(status="paused", scope_confirmed=True)
    with pytest.raises(ValueError, match="single-line"):
        session.command("correct", {"note_text": note})
    assert session.public()["scope_confirmed"]
    assert session.commands.empty()


def test_terminal_state_is_written_only_after_durable_factual_event(monkeypatch, tmp_path):
    manager = ControlledSessionManager(ROOT, tmp_path)
    session = _Session(manager, {"task": "Save local note", "note_text": "Original note"}, None)
    session.update(status="paused")
    original_update = session.update
    original_fsync = __import__("os").fsync
    flushed = []

    def fsync(fd):
        flushed.append(fd)
        return original_fsync(fd)

    def update_after_fact(**values):
        assert flushed, "Terminal event must be fsynced before state publication"
        facts = [json.loads(line) for line in (session.directory / "events.jsonl").read_text().splitlines()]
        assert facts[-1]["data"]["status"] == values["status"] == "stopped"
        assert session.events[-1] == facts[-1]
        assert json.loads((session.directory / "session.json").read_text())["status"] == "paused"
        original_update(**values)

    monkeypatch.setattr("cua_jev.ui.sessions.os.fsync", fsync)
    monkeypatch.setattr(session, "update", update_after_fact)
    session.status("stopped")
    assert json.loads((session.directory / "session.json").read_text())["status"] == "stopped"


def test_terminal_event_write_failure_cannot_publish_terminal_state(monkeypatch, tmp_path):
    manager = ControlledSessionManager(ROOT, tmp_path)
    session = _Session(manager, {"task": "Save local note", "note_text": "Original note"}, None)
    session.update(status="paused")

    def failed_event(*args, **kwargs):
        raise OSError("injected event persistence failure")

    monkeypatch.setattr(session, "event", failed_event)
    with pytest.raises(OSError, match="persistence failure"):
        session.status("stopped")
    assert session.public()["status"] == "paused"
    assert json.loads((session.directory / "session.json").read_text())["status"] == "paused"
