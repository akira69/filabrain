import asyncio
import json
import struct

import httpx
from app.api.deps import DBSession, PrincipalDep, ensure_any_permission
from fastapi import APIRouter, HTTPException, Request

from .research import is_enabled
from .settings import config_record, endpoints, microphone_revision, model_key

router = APIRouter(tags=["FilaBrain"])
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024


def validate_wav(raw: bytes) -> None:
    try:
        if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE" or struct.unpack_from("<I", raw, 4)[0] != len(raw) - 8:
            raise ValueError
        offset = 12
        chunks = {}
        while offset < len(raw):
            tag, size = struct.unpack_from("<4sI", raw, offset)
            start = offset + 8
            end = start + size
            if end > len(raw) or tag in chunks:
                raise ValueError
            if tag in (b"fmt ", b"data"):
                chunks[tag] = raw[start:end]
            offset = end + size % 2
        if offset != len(raw) or len(chunks[b"fmt "]) < 16:
            raise ValueError
        if struct.unpack_from("<HHIIHH", chunks[b"fmt "]) != (1, 1, 16000, 32000, 2, 16):
            raise ValueError
        if not 0 < len(chunks[b"data"]) <= 60 * 16000 * 2 or len(chunks[b"data"]) % 2:
            raise ValueError
    except (ValueError, KeyError, struct.error):
        raise HTTPException(422, "Use a mono 16 kHz PCM16 WAV recording of at most 60 seconds") from None


def configured_endpoint(config: dict, request: Request) -> dict:
    if not config.get("microphone_enabled", True):
        raise HTTPException(403, "Microphone dictation is disabled")
    source = config.get("microphone_endpoint_id")
    endpoint = next((item for item in endpoints(config) if item["id"] == source), None)
    if not endpoint or not endpoint["audio_enabled"] or not endpoint["model_url"] or not endpoint["audio_model_id"]:
        raise HTTPException(503, "Choose an audio-enabled microphone endpoint in FilaBrain settings")
    # These are consent guards; only the saved configuration chooses the destination.
    if (request.query_params.get("endpoint_id") != source
        or request.query_params.get("revision") != microphone_revision(config)):
        raise HTTPException(409, "Microphone settings changed. Start a new recording.")
    return endpoint


@router.post("/transcribe")
async def transcribe(request: Request, db: DBSession, principal: PrincipalDep):
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    if principal.auth_type != "session":
        raise HTTPException(403, "A browser session is required")
    await ensure_any_permission(db, principal, "filaments:read", "filaments:create", "filaments:update")
    _, config = await config_record(db)
    configured_endpoint(config, request)
    await db.rollback()
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "audio/wav":
        raise HTTPException(415, "Send the recording as audio/wav")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "The recording exceeds 2 MiB")
        raw.extend(chunk)
    validate_wav(raw)
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    _, config = await config_record(db)
    endpoint = configured_endpoint(config, request)
    await db.rollback()
    try:
        key = model_key(endpoint)
        headers = {"Accept-Encoding": "identity"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with (
            asyncio.timeout(120),
            httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False, follow_redirects=False) as client,
            client.stream("POST", f"{endpoint['model_url']}/audio/transcriptions", headers=headers,
                          files={"file": ("recording.wav", bytes(raw), "audio/wav")},
                          data={"model": endpoint["audio_model_id"], "response_format": "json"}) as response,
        ):
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise ValueError
            result = bytearray()
            async for chunk in response.aiter_raw():
                if len(result) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError
                result.extend(chunk)
        payload = json.loads(result)
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or len(text) > 32000:
            raise ValueError
        text.encode("utf-8")
    except (httpx.HTTPError, TimeoutError, ValueError, RuntimeError, TypeError):
        raise HTTPException(502, "Could not transcribe the recording. Check the audio endpoint and try again.") from None
    return {"text": text}
