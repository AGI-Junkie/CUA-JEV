"""Local-only workbench API over real controlled-browser task sessions."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .configuration import ConfigurationStore
from .sessions import ControlledSessionManager


def attach_workbench_api(app, *, root: Path, data: Path, csrf: str) -> None:
    configuration = ConfigurationStore(data / "configuration.json")
    sessions = ControlledSessionManager(root, data / "sessions", configuration=configuration)
    app.state.workbench_configuration = configuration
    app.state.workbench_sessions = sessions
    prefix = "/api/workbench"

    def get(session_id):
        try:
            return sessions.get(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found") from None

    async def body(request):
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON object") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object")
        return value

    @app.get(prefix + "/bootstrap")
    def bootstrap():
        return {"csrf": csrf, "configuration": configuration.public(),
                "sessions": sessions.list(), "capabilities": {
                    "execution": "real local headless Chromium input",
                    "model_responses": "simulated HTTP; real API execution disabled",
                    "observer": ("DOM semantics or optional screenshot API fixture; "
                                 "no real VLM or Windows validation"),
                    "observation_modes": ["dom", "mock_vision"],
                    "scope": "local note-save lab only",
                    "live_api_enabled": False,
                }}

    @app.get(prefix + "/config")
    def get_configuration():
        return configuration.public()

    @app.put(prefix + "/config")
    async def update_configuration(request: Request):
        try:
            payload = await body(request)
            await asyncio.to_thread(configuration.update, payload)
            return configuration.public()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get(prefix + "/sessions")
    def list_sessions():
        return sessions.list()

    @app.post(prefix + "/sessions", status_code=201)
    async def create_session(request: Request):
        try:
            payload = await body(request)
            return await asyncio.to_thread(sessions.create, payload)
        except ValueError as exc:
            raise HTTPException(409 if "active session" in str(exc).lower() else 400, str(exc)) from None

    @app.get(prefix + "/sessions/{session_id}")
    def get_session(session_id: str):
        return get(session_id)

    @app.get(prefix + "/sessions/{session_id}/events")
    def events(session_id: str, after: int = 0):
        get(session_id)
        if after < 0:
            raise HTTPException(400, "Event cursor must be nonnegative")
        return sessions.events(session_id, after)

    @app.get(prefix + "/sessions/{session_id}/stream")
    async def event_stream(session_id: str, request: Request, after: int = 0, once: bool = False):
        await asyncio.to_thread(get, session_id)
        try:
            cursor = int(request.headers.get("last-event-id", after))
            if cursor < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Event cursor must be nonnegative") from None

        async def generate():
            nonlocal cursor
            # Snapshot has no SSE id: replay starts at the requested cursor.
            snapshot = await asyncio.to_thread(get, session_id)
            yield "event: snapshot\ndata: " + json.dumps(snapshot, ensure_ascii=False) + "\n\n"
            while True:
                batch = await asyncio.to_thread(sessions.events, session_id, cursor)
                for event in batch["events"]:
                    cursor = event["seq"]
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: fact\ndata: {payload}\n\n"
                if once or await request.is_disconnected():
                    break
                current = await asyncio.to_thread(get, session_id)
                if current["status"] in {"completed", "stopped", "failed", "interrupted"}:
                    # Completion may have arrived after the first batch was read.
                    final = await asyncio.to_thread(sessions.events, session_id, cursor)
                    for event in final["events"]:
                        cursor = event["seq"]
                        payload = json.dumps(event, ensure_ascii=False)
                        yield f"id: {cursor}\nevent: fact\ndata: {payload}\n\n"
                    yield "event: end\ndata: {}\n\n"
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get(prefix + "/sessions/{session_id}/frame")
    def frame(session_id: str):
        get(session_id)
        try:
            return FileResponse(sessions.frame_path(session_id), media_type="image/png")
        except FileNotFoundError:
            raise HTTPException(409, "Preview frame is not ready") from None

    @app.post(prefix + "/sessions/{session_id}/{action}")
    async def command(session_id: str, action: str, request: Request):
        get(session_id)
        if action == "confirm":
            action = "confirm_scope"
        if action not in {"confirm_scope", "invalidate_scope", "start", "pause", "resume", "takeover",
                          "correct", "reobserve", "replan", "stop"}:
            raise HTTPException(404, "Unknown session command")
        try:
            payload = await body(request)
            return await asyncio.to_thread(sessions.command, session_id, action, payload)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
