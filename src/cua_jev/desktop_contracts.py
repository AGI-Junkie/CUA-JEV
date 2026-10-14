"""Coordinate-free desktop plans and fresh, unambiguous live bindings.

Contracts are JSON serializable. They retain semantic intent, never rectangles,
frame refs or pixels. Compilation is deliberately separate from model choice.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def _valid_condition(value: Any, *, allow_empty: bool = False) -> bool:
    if allow_empty and value == {}:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"kind", "value"}
        and value["kind"] in {"window_title_contains", "text_contains", "control_exists"}
        and isinstance(value["value"], str)
        and 0 < len(value["value"]) <= 160
    )


@dataclass(frozen=True)
class SceneState:
    window_handle: int
    fingerprint: str
    observed_at: float = field(default_factory=time.time)
    window_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticTarget:
    name: str
    control_type: str
    automation_id: str = ""
    visual: bool = False

    @classmethod
    def from_live(cls, target: Any) -> SemanticTarget:
        if hasattr(target, "label"):
            return cls(target.label, "visual", visual=True)
        return cls(target.name, target.control_type, target.automation_id)

    def matches(self, target: Any) -> bool:
        return self == self.from_live(target)


@dataclass(frozen=True)
class ContractOption:
    target: SemanticTarget
    operation: str
    value: str
    expected: dict[str, str]


@dataclass(frozen=True)
class PlanContract:
    subgoal: str
    window_handle: int
    options: tuple[ContractOption, ...]
    success: dict[str, str]
    contract_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def ground(cls, plan: Any, snapshot: Any) -> PlanContract:
        live = {item.ref: item for item in (*snapshot.controls, *snapshot.visual_targets)}
        return cls(
            plan.subgoal,
            snapshot.window_handle,
            tuple(
                ContractOption(
                    SemanticTarget.from_live(live[option.ref]),
                    option.operation,
                    option.value,
                    dict(option.expected),
                )
                for option in plan.options
            ),
            {"kind": plan.success_kind, "value": plan.success_value},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanContract:
        if (
            not isinstance(data, dict)
            or type(data.get("window_handle")) is not int
            or not isinstance(data.get("subgoal"), str)
            or not data["subgoal"].strip()
            or not isinstance(data.get("options"), list)
            or not 1 <= len(data["options"]) <= 16
            or not isinstance(data.get("contract_id"), str)
            or not data["contract_id"]
            or not _valid_condition(data.get("success"))
        ):
            raise ValueError("invalid persisted plan contract")
        for item in data["options"]:
            if (
                not isinstance(item, dict)
                or item.get("operation") not in {"click", "fill", "press", "scroll"}
                or not isinstance(item.get("value"), str)
                or len(item["value"]) > 1000
                or not isinstance(item.get("target"), dict)
                or any(
                    not isinstance(item["target"].get(key), str)
                    for key in ("name", "control_type", "automation_id")
                )
                or set(item["target"]) != {"name", "control_type", "automation_id", "visual"}
                or type(item["target"].get("visual")) is not bool
                or not _valid_condition(item.get("expected"), allow_empty=True)
            ):
                raise ValueError("invalid persisted contract option")
            operation, value = item["operation"], item["value"]
            if (
                operation == "press"
                and value
                not in {"tab", "escape", "up", "down", "left", "right", "home", "end", "pageup", "pagedown"}
            ) or (operation == "scroll" and value not in {"-3", "-2", "-1", "1", "2", "3"}):
                raise ValueError("invalid persisted navigation")
        return cls(
            data["subgoal"],
            data["window_handle"],
            tuple(
                ContractOption(
                    SemanticTarget(**item["target"]), item["operation"], item["value"], dict(item["expected"])
                )
                for item in data["options"]
            ),
            dict(data["success"]),
            data["contract_id"],
        )


class FreshCandidateCompiler:
    """Bind semantic options against the current scene; ambiguous means no action."""

    def bind(self, contract: PlanContract, snapshot: Any, used: set[int]) -> dict[int, Any]:
        if contract.window_handle != snapshot.window_handle:
            return {}
        live = (*snapshot.controls, *snapshot.visual_targets)
        result = {}
        for index, option in enumerate(contract.options):
            if index in used:
                continue
            matches = [
                target
                for target in live
                if option.target.matches(target) and getattr(target, "enabled", True)
            ]
            if len(matches) == 1:
                result[index] = matches[0]
        return result


def verify_postcondition(expected: dict[str, str], snapshot: Any) -> bool:
    """Verify declared effects from a new observation, independently of dispatch."""
    kind, value = expected.get("kind"), expected.get("value", "")
    if not value:
        return False
    folded = value.casefold()
    if kind == "window_title_contains":
        return folded in snapshot.window_title.casefold()
    if kind == "text_contains":
        return folded in "\n".join((snapshot.text, *snapshot.visual_text)).casefold()
    if kind == "control_exists":
        return any(folded in target.name.casefold() for target in snapshot.controls)
    return False
