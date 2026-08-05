from __future__ import annotations

from src.develop import hcx_client


class _Response:
    status_code = 200

    def json(self):
        return {
            "result": {
                "message": {"content": '{"ok": true}'},
                "usage": {"totalTokens": 1},
            }
        }


def test_structured_output_uses_service_compatible_completion_limit(monkeypatch):
    """The service app rejects 8,000 before inference, so L2 must request 4,000."""
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(hcx_client.requests, "post", fake_post)

    parsed, usage, _ = hcx_client.call_hcx_json(
        system_prompt="system",
        user_prompt="user",
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
        api_key="test-key",
        model="HCX-007",
        timeout=1,
    )

    assert captured["maxCompletionTokens"] == 4000
    assert captured["thinking"] == {"effort": "none"}
    assert parsed == {"ok": True}
    assert usage == {"totalTokens": 1}


def test_generation_settings_and_seed_are_forwarded(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(hcx_client.requests, "post", fake_post)

    hcx_client.call_hcx_json(
        system_prompt="system",
        user_prompt="user",
        schema={"type": "object", "properties": {}},
        api_key="test-key",
        model="HCX-007",
        timeout=1,
        temperature=0.0,
        top_p=0.7,
        seed=1201,
    )

    assert captured["temperature"] == 0.0
    assert captured["topP"] == 0.7
    assert captured["seed"] == 1201
