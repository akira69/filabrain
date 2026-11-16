import pytest
from test_backend import BASE, installed
from test_spool_profiles import catalog_requests

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(("selected", "follow_up", "expected"), [
    ("emptyspool-264", "What is the tare of the one I selected?", [232]),
    ("emptyspool-264", "What is the tare for the Elegoo PETG 1 kg plastic spool?", [114]),
    ("emptyspool-264", "What is the tare of the one I selected for Elegoo PETG?", []),
    ("emptyspool-101", "What is the tare of the one I selected?", []),
])
async def test_chat_verifies_selected_spool_in_latest_lookup(
    installed, catalog_requests, monkeypatch, selected, follow_up, expected,
):
    from filabrain import research

    async def check_model(*_args):
        pass

    async def complete(*_args, **_kwargs):
        return "Ordinary chat answer."

    monkeypatch.setattr(research, "check_model_catalog", check_model)
    monkeypatch.setattr(research, "request_completion", complete)
    config = {"model_url": "http://model.test", "model_id": "example"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200
    messages = [{"role": "user", "content": "What is the empty spool weight for Paramount 3D?"}]
    first = (await installed.post(f"{BASE}/chat", json={"messages": messages})).json()
    assert {card["id"] for card in first["spool_profiles"]} == {"emptyspool-263", "emptyspool-264"}

    response = await installed.post(f"{BASE}/chat", json={
        "messages": [*messages, {"role": "assistant", "content": first["reply"]},
                     {"role": "user", "content": follow_up}],
        "selected_spool_profile_id": selected,
        "selected_spool_profile": {"id": selected, "empty_weight_g": 9999},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert [card["empty_weight_g"] for card in result.get("spool_profiles", [])] == expected
    if expected:
        assert f"{expected[0]} g" in result["reply"]
    if selected == "emptyspool-101":
        assert "couldn’t verify the selected spool" in result["reply"]
    assert "9999" not in result["reply"]


    if follow_up == "What is the tare of the one I selected?" and expected:
        assert result["selected_spool_profile_id"] == selected
        response = await installed.post(f"{BASE}/chat", json={
            "messages": [
                *messages, {"role": "assistant", "content": first["reply"]},
                {"role": "user", "content": follow_up},
                {"role": "assistant", "content": result["reply"], "confirmed_spool_profile_id": selected},
                {"role": "user", "content": "How wide is it?"},
            ],
            "selected_spool_profile_id": selected,
        })
        assert response.status_code == 200, response.text
        answer = response.json()
        assert [card["id"] for card in answer["spool_profiles"]] == [selected]
        assert answer["selected_spool_profile_id"] == selected
        assert "dimensions are unavailable" in answer["reply"]

        response = await installed.post(f"{BASE}/chat", json={
            "messages": [
                *messages, {"role": "assistant", "content": first["reply"]},
                {"role": "user", "content": follow_up},
                {"role": "assistant", "content": result["reply"], "confirmed_spool_profile_id": selected},
                {"role": "user", "content": "What is the tare of the one I selected?"},
            ],
            "selected_spool_profile_id": "emptyspool-263",
            "spool_lookup_message_index": 1,
        })
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["selected_spool_profile_id"] == "emptyspool-263"
        assert "208 g empty" in answer["reply"]
