import json

import httpx
import pytest

from cua_jev.errors import PolicyError
from cua_jev.models import ActionCandidate, Channel, Observation
from cua_jev.policy import JevPolicy, RulePolicy


def candidates():
    return [
        ActionCandidate("gui", Channel.GUI, "uia.invoke", "Click a button"),
        ActionCandidate("mcp", Channel.MCP, "mcp.filesystem.read", "Read with MCP"),
    ]


def test_rule_policy_prefers_structured_read_only_channel():
    obs = Observation("read", "read file", {})
    assert RulePolicy().choose(obs, candidates()).candidate_id == "mcp"


def test_jev_policy_validates_and_returns_choice():
    def handler(request):
        assert "secret" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "action": {
                        "choice": "mcp",
                        "probabilities": {"gui": 0.1, "mcp": 0.9},
                        "confidence": 0.8,
                    }
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    policy = JevPolicy(api_key="secret", client=client, retries=0)
    decision = policy.choose(Observation("read", "read file", {}), candidates())
    assert decision.candidate_id == "mcp"
    assert decision.probabilities["mcp"] == 0.9


def test_jev_policy_rejects_inconsistent_choice():
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "action": {
                        "choice": "gui",
                        "probabilities": {"gui": 0.1, "mcp": 0.9},
                        "confidence": 0.8,
                    }
                }
            },
        )

    policy = JevPolicy(
        api_key="secret", client=httpx.Client(transport=httpx.MockTransport(handler)), retries=0
    )
    with pytest.raises(PolicyError, match="invalid Jev"):
        policy.choose(Observation("read", "read file", {}), candidates())


def test_jev_policy_retries_transient_connection_failures():
    attempts = 0
    sleeps = []

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary outage", request=request)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "action": {
                        "choice": "mcp",
                        "probabilities": {"gui": 0.1, "mcp": 0.9},
                        "confidence": 0.8,
                    }
                }
            },
        )

    policy = JevPolicy(
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retries=4,
        sleep=sleeps.append,
    )
    decision = policy.choose(Observation("read", "read file", {}), candidates())

    assert decision.candidate_id == "mcp"
    assert attempts == 3
    assert sleeps == [0.5, 1.0]


def test_jev_policy_reports_actionable_connection_error():
    def handler(request):
        raise httpx.ConnectError("secret network details", request=request)

    policy = JevPolicy(
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retries=1,
        sleep=lambda _: None,
    )
    with pytest.raises(PolicyError, match="temporarily unreachable after 2 attempts; retry") as error:
        policy.choose(Observation("read", "read file", {}), candidates())
    assert "secret network details" not in str(error.value)


def test_jev_policy_rejects_rule_fallback_configuration():
    with pytest.raises(PolicyError, match="rule fallback is disabled"):
        JevPolicy(api_key="secret", fallback_on_transport=True)


def test_jev_policy_never_falls_back_on_authentication_failure():
    def handler(_request):
        return httpx.Response(401, json={"detail": "unauthorized"})

    policy = JevPolicy(
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retries=2,
    )
    with pytest.raises(PolicyError, match="authentication failed"):
        policy.choose(Observation("read", "read file", {}), candidates())
    assert "fallback" not in policy.last_exchange


def test_jev_timeout_records_attempts_latency_and_no_fallback():
    def handler(request):
        raise httpx.ReadTimeout("private transport detail", request=request)

    policy = JevPolicy(api_key="secret", retries=1, sleep=lambda _: None,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PolicyError, match="timed out after 2 attempts"):
        policy.choose(Observation("read", "read file", {}), candidates())
    assert policy.metrics["calls"] == 1
    assert policy.metrics["attempts"] == 2
    assert policy.metrics["transport_failures"] == 1
    assert policy.metrics["latency_ms"] > 0
    assert policy.last_exchange["latency_ms"] > 0
    assert "fallback" not in policy.last_exchange
    assert "private transport detail" not in str(policy.last_exchange)


@pytest.mark.parametrize("malformed", [
    None, [], 42, "not-an-object", {"answers": []}, {"answers": {"action": []}},
    {"answers": {"action": {"choice": "gui", "probabilities": ["gui", "mcp"], "confidence": 1}}},
    {"answers": {"action": {"choice": "gui", "probabilities": {"gui": 1, "mcp": 0}, "confidence": True}}},
    {"answers": {"action": {"choice": "gui", "probabilities": {"gui": True, "mcp": 0}, "confidence": 1}}},
    {"answers": {"action": {"choice": [], "probabilities": {"gui": 1, "mcp": 0}, "confidence": 1}}},
    {"model": [], "answers": {"action": {"choice": "gui", "probabilities": {"gui": 1, "mcp": 0},
                                        "confidence": 1}}},
    {"answers": {"action": {"choice": "gui", "probabilities": {"gui": 1, "mcp": 0},
                            "confidence": 10**400}}},
    {"answers": {"action": {"choice": "gui", "probabilities": {"gui": 10**400, "mcp": 0},
                            "confidence": 1}}},
])
def test_malformed_jev_http_response_is_sanitized_and_never_executes(malformed):
    from cua_jev.guard import ActionGuard
    from cua_jev.registry import ExecutorRegistry
    from cua_jev.runtime import AgentRuntime

    policy = JevPolicy(api_key="fixture-key", retries=0,
                       client=httpx.Client(transport=httpx.MockTransport(
                           lambda _request: httpx.Response(200, content=json.dumps(malformed),
                                                         headers={"content-type": "application/json"}))))
    registry = ExecutorRegistry()
    emitted = []
    registry.register(Channel.GUI, lambda *args: emitted.append(args))
    runtime = AgentRuntime(policy=policy, guard=ActionGuard(), executors=registry, require_jev=True)
    with pytest.raises(PolicyError, match="invalid Jev choice response; no action executed"):
        runtime.step(Observation("fixture", "choose", {}), candidates())
    assert emitted == []
    assert runtime.metrics["policy_calls"] == runtime.metrics["policy_errors"] == 1
    assert policy.metrics["calls"] == policy.metrics["attempts"] == 1
    assert "fallback" not in policy.last_exchange
