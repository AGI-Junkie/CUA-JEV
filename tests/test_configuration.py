import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from cua_jev.ui.configuration import (
    BudgetExceeded,
    BudgetLedger,
    ConfigurationError,
    ConfigurationStore,
    RunLimits,
)


def test_memory_keys_atomic_private_persistence_and_immutable_snapshot(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigurationStore(path)
    public = store.update({"roles": {"jev": {"name": "jev", "api_key": "private-secret"}}})
    snapshot = store.snapshot()
    assert public["roles"]["jev"]["key_configured"]
    assert not public["roles"]["jev"]["verified"]
    assert "private-secret" not in json.dumps(public)
    assert "private-secret" not in path.read_text()
    assert "private-secret" not in repr(snapshot)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Windows ACLs require separate validation; POSIX mode bits do not prove them.
    assert ConfigurationStore(path).public()["roles"]["jev"]["key_configured"] is False
    store.update({"roles": {"jev": {"api_key": "", "name": "new"}}})
    assert store.snapshot().roles["jev"].api_key == "private-secret"
    assert snapshot.roles["jev"].name == "jev"
    with pytest.raises(TypeError):
        snapshot.roles["vision"] = snapshot.roles["jev"]
    with pytest.raises(FrozenInstanceError):
        snapshot.roles["jev"].name = "modified"
    store.update({"roles": {"jev": {"clear_api_key": True}}})
    assert store.public()["roles"]["jev"]["key_configured"] is False
    assert not list(tmp_path.glob(".configuration-*"))


@pytest.mark.parametrize("endpoint", [
    "http://remote.example/v1", "https://user:secret@example.com", "https://example.com?q=secret",
    "https://example.com#secret", "https://example.com:wrong", "ftp://localhost", "https://x\\y",
])
def test_unsafe_endpoints_rejected_without_reflecting_input(tmp_path, endpoint):
    store = ConfigurationStore(tmp_path / "config.json")
    with pytest.raises(ConfigurationError) as error:
        store.update({"roles": {"jev": {"endpoint": endpoint, "api_key": "private-secret"}}})
    assert endpoint not in str(error.value)
    assert "private-secret" not in str(error.value)
    assert not store.public()["roles"]["jev"]["key_configured"]


@pytest.mark.parametrize("endpoint", ["https://example.com/v1", "http://localhost:8111", "http://127.0.0.1"])
def test_https_or_loopback_mock_only(tmp_path, endpoint):
    store = ConfigurationStore(tmp_path / "config.json")
    store.update({"roles": {"planner": {"endpoint": endpoint}}})
    with pytest.raises(ConfigurationError):
        store.update({"mode": "live"})
    assert not store.public()["live_execution_enabled"]


@pytest.mark.parametrize("limits", [
    {"max_steps": 101}, {"max_steps": True}, {"max_wall_seconds": 601},
    {"max_model_attempts": 21}, {"max_retries": 3}, {"decision_timeout_seconds": 31},
    {"decision_timeout_seconds": float("nan")}, {"max_steps": 1.5},
])
def test_bounded_limits(tmp_path, limits):
    store = ConfigurationStore(tmp_path / "config.json")
    with pytest.raises(ConfigurationError):
        store.update({"limits": limits})


def test_hand_edited_disk_cannot_load_credentials(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"roles": {"jev": {"api_key": "disk-secret", "name": "jev"}}}))
    assert not ConfigurationStore(path).snapshot().roles["jev"].api_key


def test_failed_save_preserves_memory_and_previous_file(tmp_path, monkeypatch):
    store = ConfigurationStore(tmp_path / "config.json")
    store.update({"roles": {"jev": {"name": "old", "api_key": "old-key"}}})
    previous = store.path.read_text()

    def fail(*args):
        raise OSError("failed replacement")

    monkeypatch.setattr("cua_jev.ui.configuration.os.replace", fail)
    with pytest.raises(OSError):
        store.update({"roles": {"jev": {"name": "new", "api_key": "new-key"}}})
    assert store.snapshot().roles["jev"].name == "old"
    assert store.path.read_text() == previous
    assert not list(tmp_path.glob(".configuration-*"))


def test_budget_counts_retries_and_blocks_cancelled_or_late_dispatch():
    now = [0.0]
    cancelled = [False]
    ledger = BudgetLedger(RunLimits(max_steps=1, max_model_attempts=2, max_wall_seconds=3),
                          clock=lambda: now[0], cancelled=lambda: cancelled[0])
    ledger.record_model_attempt("planner")
    ledger.record_model_attempt("planner", retry=1)
    with pytest.raises(BudgetExceeded, match="attempt"):
        ledger.record_model_attempt("jev")
    ledger.record_step()
    with pytest.raises(BudgetExceeded, match="Step"):
        ledger.record_step()
    now[0] = 3
    with pytest.raises(BudgetExceeded, match="wall"):
        ledger.check()
    now[0] = 0
    cancelled[0] = True
    with pytest.raises(BudgetExceeded, match="cancelled"):
        ledger.check()
    assert ledger.public()["model_attempts"] == 2
    assert ledger.public()["estimated_cost_dollars"] is None


def test_concurrent_reservations_cannot_overrun_budget():
    ledger = BudgetLedger(RunLimits(max_model_attempts=5))

    def reserve(_):
        try:
            ledger.record_model_attempt("jev")
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(30))) == 5
    assert ledger.public()["model_attempts"] == 5


def test_costs_only_user_estimates_never_enforced_ceiling(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    store.update({"cost_ceiling_dollars": 0.01,
                  "roles": {"jev": {"estimated_dollars_per_attempt": 0.25}}})
    snapshot = store.snapshot()
    ledger = BudgetLedger(snapshot.limits, roles=snapshot.roles)
    ledger.record_model_attempt("jev")
    assert ledger.public()["estimated_cost_dollars"] == 0.25
    assert ledger.public()["cost_status"] == "user_estimate"
    assert not ledger.public()["cost_ceiling_enforced"]
    assert not store.public()["cost"]["ceiling_enforced"]
    ledger.record_model_attempt("planner")
    assert ledger.public()["estimated_cost_dollars"] is None
    with pytest.raises(BudgetExceeded, match="retry"):
        ledger.record_model_attempt("jev", retry=3)


@pytest.mark.parametrize("retry", [True, 0.5, -1, 3])
def test_retry_argument_bounded_integer(retry):
    ledger = BudgetLedger(RunLimits())
    with pytest.raises(BudgetExceeded):
        ledger.record_model_attempt("jev", retry=retry)
    assert ledger.public()["model_attempts"] == 0


def test_jev_loopback_http_rejected_atomically_without_secret_reflection(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    store.update({"roles": {"jev": {"name": "previous", "api_key": "previous-key"}}})
    previous_file = store.path.read_text()
    previous_snapshot = store.snapshot()
    endpoint = "http://localhost:8111/private-secret-path"
    with pytest.raises(ConfigurationError, match="Jev endpoint requires HTTPS") as error:
        store.update({"roles": {
            "planner": {"name": "uncommitted"},
            "jev": {"endpoint": endpoint, "api_key": "submitted-secret"},
        }})
    assert endpoint not in str(error.value)
    assert "submitted-secret" not in str(error.value)
    assert store.snapshot() is previous_snapshot
    assert store.path.read_text() == previous_file
    assert store.snapshot().roles["jev"].api_key == "previous-key"


def test_vision_loopback_http_remains_valid_in_controlled_mock(tmp_path):
    store = ConfigurationStore(tmp_path / "config.json")
    store.update({"roles": {"vision": {"endpoint": "http://127.0.0.1:8111/v1"}}})
    assert store.snapshot().roles["vision"].endpoint == "http://127.0.0.1:8111/v1"
