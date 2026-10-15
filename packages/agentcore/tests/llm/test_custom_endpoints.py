import asyncio
import json

import httpx
import pytest

from agentcore.drivers.api import ApiDriver
from agentcore.drivers.vanilla import VanillaDriver
from agentcore.llm.endpoints import api_key_base_url, normalize_base_url
from agentcore.llm.router import LLMRouter
from agentcore.models import AgentConfig, ResolvedLimits, TaskBody


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://token@host/v1",
        "https://host/?key=secret",
        "https://host/#fragment",
        "http://host:99999",
        "http://host/\nkey",
        123,
    ],
)
def test_endpoint_validation_is_shared_across_drivers(url):
    with pytest.raises(ValueError):
        normalize_base_url(url)


def test_subscription_metadata_cannot_redirect_its_token():
    assert api_key_base_url("oauth_subscription", {"base_url": "https://untrusted/v1"}) is None
    assert api_key_base_url("api_key", {"base_url": "http://gateway/api/"}) == "http://gateway/api"


async def test_request_scoped_clients_keep_two_tenants_separate(respx_mock):
    router = LLMRouter()
    calls = []

    def respond(request):
        calls.append(
            (str(request.url), request.headers["authorization"], json.loads(request.content))
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    first = respx_mock.post("https://first.example/v1/chat/completions").mock(side_effect=respond)
    second = respx_mock.post("http://second.example/api/chat/completions").mock(side_effect=respond)

    async def call(base, key):
        client, model = router.route("gpt-4o", base_url=base)
        return await client.create(
            model=model, system="x", messages=[], tools=[], max_tokens=100, credential=key
        )

    results = await asyncio.gather(
        call("https://first.example/v1", "first-key"),
        call("http://second.example/api", "second-key"),
    )
    assert all(result.content[0]["text"] == "ok" for result in results)
    assert first.called and second.called
    assert {(url, auth) for url, auth, _ in calls} == {
        ("https://first.example/v1/chat/completions", "Bearer first-key"),
        ("http://second.example/api/chat/completions", "Bearer second-key"),
    }
    assert all(body["max_tokens"] == 100 for _, _, body in calls)
    assert router.route("gpt-4o")[0]._base_url == "https://api.openai.com/v1"


@pytest.mark.parametrize("driver_type", [ApiDriver, VanillaDriver])
async def test_driver_uses_custom_base_and_key_on_the_actual_request(
    driver_type, tmp_path, respx_mock
):
    def respond(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer gateway-key"
        assert body["model"] == "gpt-4o"
        assert "max_tokens" in body
        message = {"content": "gateway-reply"}
        reason = "stop"
        if driver_type is VanillaDriver:
            message = {
                "tool_calls": [
                    {
                        "id": "done_1",
                        "type": "function",
                        "function": {
                            "name": "done",
                            "arguments": json.dumps({"success": True, "output": "gateway-reply"}),
                        },
                    }
                ]
            }
            reason = "tool_calls"
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": reason}]}
        )

    route = respx_mock.post("http://gateway.example/api/chat/completions").mock(side_effect=respond)
    driver = driver_type(router=LLMRouter())

    async def emit(*args):
        pass

    result = await driver.run(
        task=TaskBody(prompt="hi"),
        config=AgentConfig(driver=driver.name, model="gpt-4o"),
        limits=ResolvedLimits(max_iterations=3, max_tokens=10000, timeout_seconds=3),
        credential="gateway-key",
        credential_meta={"base_url": "http://gateway.example/api/"},
        emit=emit,
        cancel=asyncio.Event(),
        workspace=str(tmp_path),
    )
    assert result.success and "gateway-reply" in str(result.output)
    assert route.call_count == 1


async def test_gateway_failure_never_falls_back_to_official_openai(tmp_path, respx_mock):
    custom = respx_mock.post("https://broken.example/v1/chat/completions").respond(
        401, json={"error": {"message": "bad key"}}
    )
    official = respx_mock.post("https://api.openai.com/v1/chat/completions").respond(200)

    async def emit(*args):
        pass

    result = await ApiDriver(router=LLMRouter()).run(
        task=TaskBody(prompt="hi"),
        config=AgentConfig(driver="api", model="gpt-4o"),
        limits=ResolvedLimits(max_iterations=1, max_tokens=10000, timeout_seconds=3),
        credential="gateway-key",
        credential_meta={"base_url": "https://broken.example/v1"},
        emit=emit,
        cancel=asyncio.Event(),
        workspace=str(tmp_path),
    )
    assert not result.success and custom.called and not official.called
