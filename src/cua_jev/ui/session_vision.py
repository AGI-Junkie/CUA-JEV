"""Local screenshot API fixture. All vision inference here is explicitly simulated.

Actual JPEG requests pass through ChatModelPlanner's production serialization and
response validation. DOM semantics provide fixture answers; no pixel recognition
or external model verification is claimed. MockTransport cannot send a network
request, including when a configured endpoint/model name is supplied.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx

from ..controlled_browser import BrowserWindow
from ..model_planner import ChatModelPlanner
from .configuration import ModelRoleConfig


def create_mock_vision(
    page: Any,
    modelrole: ModelRoleConfig,
    on_attempt: Callable[[str], None],
    on_event: Callable[[str, dict[str, Any]], None],
) -> tuple[ChatModelPlanner, httpx.Client]:
    """Return a screenshot-capable planner and its caller-owned local HTTP client.

    ``on_attempt`` reserves the configured vision budget before any mock response
    work. Metadata records a decoded-pixel digest, never image bytes or API keys.
    The selected endpoint/name are recognized; credentials are unnecessary and
    intentionally omitted from the simulated transport.
    """
    from PIL import Image

    model = modelrole.name or "simulated-vision-fixture"
    endpoint = modelrole.endpoint or "https://vision.mock.invalid/v1"

    def transport(request: httpx.Request) -> httpx.Response:
        on_attempt("vision")
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            return httpx.Response(400, json={"error": "mock vision accepts screenshot completion only"})
        try:
            body = json.loads(request.content)
            parts = body["messages"][1]["content"]
            urls = [part["image_url"]["url"] for part in parts if part.get("type") == "image_url"]
            if len(urls) != 1 or not urls[0].startswith("data:image/jpeg;base64,"):
                raise ValueError("one JPEG data URL is required")
            raw = base64.b64decode(urls[0].split(",", 1)[1], validate=True)
            if not raw or len(raw) > 2_000_000:
                raise ValueError("empty or oversized screenshot")
            with Image.open(io.BytesIO(raw)) as decoded:
                if decoded.format != "JPEG":
                    raise ValueError("request image must decode as JPEG")
                image = decoded.convert("RGB")
                width, height = image.size
                if width < 40 or height < 40 or width * height > 16_000_000:
                    raise ValueError("screenshot dimensions outside safe bounds")
                pixel_digest = hashlib.sha256(image.tobytes()).hexdigest()
            if body.get("model") != model:
                raise ValueError("mock model role mismatch")
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error, OSError):
            return httpx.Response(400, json={"error": "invalid local screenshot request"})

        window = BrowserWindow(page)
        bounds = window.rectangle()
        # Boxes bind to the current DOM viewport, with no saved coordinates/IDs.
        # BrowserWindow excludes password inputs and returns visible semantics.
        controls = [control for control in window.descendants() if control.is_visible()]
        actionable = []
        visible_text = [window.window_text()[:160]]
        for control in controls:
            label = control.window_text().strip()
            if (
                not label
                or len(label) > 100
                or any(token in label.casefold() for token in ("password", "secret", "token", "api key"))
            ):
                continue
            if control.element_info.control_type == "Text":
                visible_text.append(label[:160])
            elif control.is_enabled():
                rect = control.rectangle()
                box = [
                    round(1000 * rect.left / bounds.right),
                    round(1000 * rect.top / bounds.bottom),
                    round(1000 * rect.right / bounds.right),
                    round(1000 * rect.bottom / bounds.bottom),
                ]
                if (
                    0 <= box[0] < box[2] <= 1000
                    and 0 <= box[1] < box[3] <= 1000
                    and box[2] - box[0] >= 5
                    and box[3] - box[1] >= 5
                ):
                    actionable.append({"label": label, "box": box})
        counts = Counter(item["label"] for item in actionable)
        targets = [item for item in actionable if counts[item["label"]] == 1][:12]
        scene = {
            "summary": "Simulated vision: current local browser DOM grounds screenshot targets.",
            "visible_text": list(dict.fromkeys(visible_text))[:24],
            "targets": targets,
        }
        on_event(
            "vision_mock",
            {
                "role": "vision",
                "model": model,
                "simulated": True,
                "transport": "httpx.MockTransport",
                "external_network": False,
                "pixel_sha256": pixel_digest,
                "jpeg_bytes": len(raw),
                "image_size": [width, height],
                "target_count": len(targets),
            },
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(scene, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
    try:
        planner = ChatModelPlanner(endpoint, model, client=client, allow_insecure_http=True)
    except Exception:
        client.close()
        raise
    return planner, client
