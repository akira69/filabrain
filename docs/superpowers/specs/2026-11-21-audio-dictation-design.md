# Optional microphone transcription

Approved direction: each named endpoint explicitly supports Chat, Audio transcription, or both. Existing endpoints remain Chat-only. Endpoints supporting both use separate chat and transcription model IDs, with the same URL/key. Audio-only endpoints never appear in the chat picker or become the chat/research default.

Microphone settings retain the enable switch and select Browser dictation or an audio-capable endpoint. Browser recognition may process audio online. Endpoint recording goes through FilaMan to the selected endpoint's standard `/audio/transcriptions` API. Both paths create editable text; only Send submits it to chat. OS keyboard dictation remains independent; the web app cannot launch it directly.

Use browser APIs and existing HTTP/config/credential handling; no audio libraries, bundled models, streaming conversation, or audio storage. Capture is opt-in and limited to 60 seconds; normalize recordings to mono 16 kHz PCM WAV for provider compatibility. Cancel on navigation, disabled settings, typing, or sending. Enforce bounded input/output and endpoint capability on the server; never silently fall back to browser/cloud processing.
