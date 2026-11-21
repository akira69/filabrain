import gzip
import io
import wave

import httpx
import pytest
from app.core.security import Principal
from fastapi import HTTPException
from test_backend import BASE, installed

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio


def wav_bytes(*, frames=160, rate=16000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\0" * frames * width * channels)
    return output.getvalue()


def audio_endpoint(**values):
    return {"id": "audio", "name": "Audio server", "model_url": "https://audio.test/v1",
            "chat_enabled": False, "audio_enabled": True, "audio_model_id": "whisper-1", **values}


async def save_audio(installed, **values):
    current = (await installed.get(f"{BASE}/settings")).json()
    response = await installed.put(f"{BASE}/settings", json={
        "model_endpoints": [audio_endpoint()], "default_endpoint_id": None,
        "microphone_endpoint_id": "audio", "model_revision": current["model_revision"], **values,
    })
    assert response.status_code == 200, response.text
    return response.json()


async def post_audio(installed, **options):
    status = (await installed.get(f"{BASE}/status")).json()
    params = {"endpoint_id": status["microphone_endpoint_id"], "revision": status["microphone_revision"],
              **options.pop("params", {})}
    return await installed.post(f"{BASE}/transcribe", params=params, **options)


async def test_endpoint_capabilities_migrate_legacy_and_exclude_audio_from_chat(installed):
    await installed.put(f"{BASE}/settings", json={"model_url": "https://chat.test/v1", "model_id": "chat"})
    legacy, = (await installed.get(f"{BASE}/settings")).json()["model_endpoints"]
    assert (legacy["chat_enabled"], legacy["audio_enabled"], legacy["audio_model_id"]) == (True, False, "")
    saved = await save_audio(installed)
    assert saved["default_endpoint_id"] is None
    assert saved["model_url"] == saved["model_id"] == ""
    assert saved["model_endpoints"][0]["model_id"] == ""
    catalog = (await installed.get(f"{BASE}/models")).json()
    assert catalog["groups"] == [] and catalog["default_model_id"] is None
    status = (await installed.get(f"{BASE}/status")).json()
    assert status["microphone_endpoint_id"] == "audio"
    assert status["microphone_endpoint_name"] == "Audio server"
    assert status["chat_endpoint_id"] is None and status["chat_model"] is None
    chat = {"messages": [{"role": "user", "content": "What is PLA?"}], "endpoint_id": "audio", "model_id": "whisper-1"}
    assert (await installed.post(f"{BASE}/chat", json=chat)).status_code == 422
    from filabrain.settings import select_model
    with pytest.raises(HTTPException) as invalid:
        select_model(saved, "whisper-1", "audio")
    assert invalid.value.status_code == 422


@pytest.mark.parametrize("changes", [
    {"default_endpoint_id": "audio"},
    {"model_endpoints": [audio_endpoint(audio_enabled=False)]},
    {"model_endpoints": [audio_endpoint(audio_model_id="")]},
    {"model_endpoints": [audio_endpoint(audio_model_id="white space")]},
    {"model_endpoints": [audio_endpoint(chat_enabled="false")]},
    {"microphone_endpoint_id": "missing"},
])
async def test_rejects_invalid_capability_and_source_settings(installed, changes):
    response = await installed.put(f"{BASE}/settings", json={
        "model_endpoints": [audio_endpoint()], "default_endpoint_id": None, **changes,
    })
    assert response.status_code == 422


async def test_source_is_preserved_and_must_be_explicitly_replaced_when_removed(installed, db_session):
    saved = await save_audio(installed)
    disabled = await installed.put(f"{BASE}/settings", json={"microphone_enabled": False, "model_revision": saved["model_revision"]})
    assert disabled.json()["microphone_endpoint_id"] == "audio"
    removed = {"model_endpoints": [], "default_endpoint_id": None}
    assert (await installed.put(f"{BASE}/settings", json=removed)).status_code == 422
    await db_session.rollback()
    assert (await installed.get(f"{BASE}/settings")).json()["microphone_endpoint_id"] == "audio"
    changed = await installed.put(f"{BASE}/settings", json=removed | {"microphone_endpoint_id": None, "model_revision": disabled.json()["model_revision"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["microphone_endpoint_id"] is None


async def test_transcription_uses_only_saved_endpoint_and_secret(installed, monkeypatch):
    from filabrain import settings
    monkeypatch.setattr(settings.settings, "oidc_enc_key", "configured")
    monkeypatch.setattr(settings, "encrypt_secret", lambda key: "encrypted:" + key)
    monkeypatch.setattr(settings, "decrypt_secret", lambda key: key.removeprefix("encrypted:"))
    saved = await save_audio(installed, model_endpoints=[audio_endpoint(api_key="private-key")])
    from filabrain import audio
    original_client = httpx.AsyncClient
    calls = []

    async def upstream(request):
        calls.append(request)
        assert str(request.url) == "https://audio.test/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer private-key"
        assert request.headers["accept-encoding"] == "identity"
        body = await request.aread()
        assert b'name="model"\r\n\r\nwhisper-1' in body
        assert b'name="response_format"\r\n\r\njson' in body
        assert b'filename="recording.wav"' in body and wav_bytes() in body
        return httpx.Response(200, stream=httpx.ByteStream(b'{"text":" Editable draft "}'))

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        assert options["timeout"].connect == 5 and options["timeout"].read == 120
        return original_client(transport=httpx.MockTransport(upstream), **options)

    monkeypatch.setattr(audio.httpx, "AsyncClient", client)
    response = await post_audio(installed, params={"model_id": "evil", "url": "https://evil.test"}, content=wav_bytes(), headers={"Content-Type": "audio/wav"})
    assert response.status_code == 200, response.text
    assert response.json() == {"text": " Editable draft "}
    assert len(calls) == 1
    assert "private-key" not in str(saved)


@pytest.mark.parametrize(("body", "content_type", "code"), [
    (wav_bytes(), "application/json", 415),
    (b"not a WAV", "audio/wav", 422),
    (wav_bytes()[:-1], "audio/wav", 422),
    (wav_bytes(frames=0), "audio/wav", 422),
    (wav_bytes(rate=8000), "audio/wav", 422),
    (wav_bytes(channels=2), "audio/wav", 422),
    (wav_bytes(width=1), "audio/wav", 422),
    (wav_bytes(frames=960001), "audio/wav", 422),
    (b"x" * (2 * 1024 * 1024 + 1), "audio/wav", 413),
])
async def test_rejects_unusable_audio_before_forwarding(installed, body, content_type, code):
    await save_audio(installed)
    response = await post_audio(installed, content=body, headers={"Content-Type": content_type})
    assert response.status_code == code, response.text


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="private provider details"),
    httpx.Response(302, headers={"Location": "https://evil.test"}),
    httpx.Response(200, text="not JSON"),
    httpx.Response(200, text="[" * 100000 + "0" + "]" * 100000),
    httpx.Response(200, json={"text": 1}),
    httpx.Response(200, content=b'{"text":"\\ud800"}'),
    httpx.Response(200, json={"text": "x" * 32001}),
    httpx.Response(200, content=b" " * (256 * 1024 + 1)),
    httpx.Response(200, content=gzip.compress(b'{"text":"draft"}'), headers={"Content-Encoding": "gzip"}),
])
async def test_upstream_failures_are_bounded_and_private(installed, monkeypatch, response):
    await save_audio(installed)
    from filabrain import audio
    original_client = httpx.AsyncClient
    monkeypatch.setattr(audio.httpx, "AsyncClient", lambda **options: original_client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(response.status_code,
            stream=httpx.ByteStream(response.content), headers=response.headers)), **options))
    result = await post_audio(installed, content=wav_bytes(), headers={"Content-Type": "audio/wav"})
    assert result.status_code == 502
    assert "private provider details" not in result.text


async def test_transcription_requires_enabled_source_and_rechecks_capability(installed, db_session):
    from filabrain.db import FilamentResearchSettings
    assert (await post_audio(installed, content=wav_bytes(), headers={"Content-Type": "audio/wav"})).status_code == 503
    await save_audio(installed, microphone_enabled=False)
    assert (await post_audio(installed, content=wav_bytes(), headers={"Content-Type": "audio/wav"})).status_code == 403
    row = await db_session.get(FilamentResearchSettings, 1)
    config = row.config | {"microphone_enabled": True}
    config["model_endpoints"] = [config["model_endpoints"][0] | {"audio_enabled": False}]
    row.config = config
    await db_session.commit()
    assert (await post_audio(installed, content=wav_bytes(), headers={"Content-Type": "audio/wav"})).status_code == 503


@pytest.mark.parametrize(("auth_type", "scopes", "code"), [
    ("token", ["filaments:read"], 403),
    ("session", ["spools:read"], 403),
    ("session", ["filaments:read"], 503),
    ("session", ["filaments:create"], 503),
    ("session", ["filaments:update"], 503),
])
async def test_transcription_permissions(installed, db_session, auth_type, scopes, code):
    await installed.get(f"{BASE}/settings")
    from filabrain.audio import transcribe
    from starlette.requests import Request
    principal = Principal(auth_type=auth_type, is_superadmin=True, scopes=scopes)
    with pytest.raises(HTTPException) as failure:
        await transcribe(Request({"type": "http", "headers": [(b"content-type", b"audio/wav")]}), db_session, principal)
    assert failure.value.status_code == code


@pytest.mark.parametrize("params", [{"endpoint_id": "evil"}, {"revision": "stale"}])
async def test_source_consent_guards_prevent_forwarding_after_settings_change(installed, monkeypatch, params):
    await save_audio(installed)
    from filabrain import audio
    def forbidden_client(**_options):
        pytest.fail("A stale recording must never reach an endpoint")
    monkeypatch.setattr(audio.httpx, "AsyncClient", forbidden_client)
    response = await post_audio(installed, params=params, content=wav_bytes(), headers={"Content-Type": "audio/wav"})
    assert response.status_code == 409


async def test_source_change_during_upload_cannot_forward_recording(installed, db_session, monkeypatch):
    saved = await save_audio(installed)
    from filabrain import audio
    from filabrain.db import FilamentResearchSettings
    from starlette.requests import Request

    async def receive():
        row = await db_session.get(FilamentResearchSettings, 1)
        row.config = row.config | {"model_revision": "changed-during-upload"}
        await db_session.commit()
        return {"type": "http.request", "body": wav_bytes(), "more_body": False}

    def forbidden_client(**_options):
        pytest.fail("A changed destination must never receive the recording")

    monkeypatch.setattr(audio.httpx, "AsyncClient", forbidden_client)
    request = Request({"type": "http", "headers": [(b"content-type", b"audio/wav")],
                       "query_string": f"endpoint_id=audio&revision={saved['microphone_revision']}".encode()}, receive)
    with pytest.raises(HTTPException) as failure:
        await audio.transcribe(request, db_session, Principal(auth_type="session", is_superadmin=True))
    assert failure.value.status_code == 409


async def test_dual_capability_endpoint_keeps_chat_and_audio_models_separate(installed):
    endpoint = audio_endpoint(chat_enabled=True, model_id="chat-model", models=["chat-model"])
    saved = await save_audio(installed, model_endpoints=[endpoint], default_endpoint_id="audio")
    assert saved["model_id"] == "chat-model"
    catalog = (await installed.get(f"{BASE}/models")).json()
    assert catalog["groups"][0]["models"] == ["chat-model"]
    assert saved["model_endpoints"][0]["audio_model_id"] == "whisper-1"
    response = await installed.put(f"{BASE}/settings", json={"model_endpoints": [endpoint], "default_endpoint_id": None})
    assert response.status_code == 422


async def test_microphone_edits_require_current_revision_and_noops_preserve_it(installed, db_session):
    original = (await installed.get(f"{BASE}/settings")).json()
    assert (await installed.put(f"{BASE}/settings", json={"microphone_enabled": False})).status_code == 409
    await db_session.rollback()
    saved = await installed.put(f"{BASE}/settings", json={"microphone_enabled": False, "model_revision": original["model_revision"]})
    assert saved.status_code == 200
    assert saved.json()["model_revision"] != original["model_revision"]
    unchanged = await installed.put(f"{BASE}/settings", json={"microphone_enabled": False, "model_revision": original["model_revision"]})
    assert unchanged.json()["model_revision"] == saved.json()["model_revision"]
    stale = await installed.put(f"{BASE}/settings", json={"microphone_enabled": True, "model_revision": original["model_revision"]})
    assert stale.status_code == 409
    await db_session.rollback()
    assert (await installed.put(f"{BASE}/settings", json={"supplier_download_mib": 12})).json()["microphone_enabled"] is False


async def test_effective_microphone_revision_rejects_changed_deployment_destination(installed, monkeypatch):
    monkeypatch.setenv("FILABRAIN_MODEL_URL", "https://original.test/v1")
    monkeypatch.setenv("FILABRAIN_MODEL_ID", "chat")
    initial = (await installed.get(f"{BASE}/settings")).json()
    endpoint, = initial["model_endpoints"]
    saved = await save_audio(installed, model_endpoints=[endpoint | {"audio_enabled": True, "audio_model_id": "whisper-1"}],
                             default_endpoint_id=endpoint["id"], microphone_endpoint_id=endpoint["id"])
    monkeypatch.setenv("FILABRAIN_MODEL_URL", "https://changed.test/v1")
    current = (await installed.get(f"{BASE}/settings")).json()
    assert current["model_revision"] == saved["model_revision"]
    assert current["microphone_revision"] != saved["microphone_revision"]
    response = await post_audio(installed, params={"revision": saved["microphone_revision"]},
                                content=wav_bytes(), headers={"Content-Type": "audio/wav"})
    assert response.status_code == 409
