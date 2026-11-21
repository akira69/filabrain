# Audio Dictation Implementation Plan

Goal: Add explicit endpoint capabilities and optional endpoint transcription to the existing microphone setting.
Spec: docs/superpowers/specs/2026-11-21-audio-dictation-design.md
Architecture: Existing endpoints and encrypted credentials remain the source of truth. Add endpoint capability fields and one bounded raw-WAV transcription route. Use browser capture/decode/resampling APIs to create a portable WAV; insert returned text into the current draft.

Interfaces:
- Endpoint: chat_enabled:boolean=true, audio_enabled:boolean=false, audio_model_id:string=''; preserve existing model_id as chat model. At least one capability. Chat default must be chat-enabled, or null if no chat endpoints. Audio-only endpoint needs no chat model_id.
- Settings: microphone_enabled:boolean=true (existing), microphone_endpoint_id:string|null=null (null means Browser). Selected source must be audio-enabled; removing/disabling a selected source requires choosing another source explicitly.
- Status/event: microphone_enabled:boolean, microphone_endpoint_id:string|null, microphone_endpoint_name:string|null. No secrets.
- POST /api/v1/filabrain/transcribe: session + same permissions as chat/research; config-selected endpoint only; Content-Type audio/wav; raw bounded mono16kHz16bit PCM WAV up to60s/2MiB; returns {text:string}. Reject disabled/browser source/unusable endpoint, no fallback. Forward multipart file/model/response_format=json with stored key; response bounded, timeout120s. No audio persisted/logged.

Tasks:
- [x] Backend capabilities, chat filtering/default validation, source setting/status with opaque effective-destination consent revision, bounded transcription API and regression tests (backend owner).
- [x] Endpoint Chat/Audio checkboxes, separate audio model field, microphone source select from audio endpoints, existing Save dirty behavior; no automatic source fallback (settings owner).
- [x] Browser recording, WAV conversion, cancellation, permission/failure feedback; integrate dictation config and localization (primary).
- [x] Integrate tests for capabilities, audio payload, no autosend, cancellation races, mic disabled, phone layout. Full browser/backend checks, fresh broad review, fix findings.
- [ ] Commit coherent changes to PR20, ZIP, update isolated LAN preview; inventory unchanged. Verify UI and settings; physical microphone/live transcription only if hardware/provider actually available.

Review focus: delayed getUserMedia after navigation; stale endpoint edits/removal and no fallback; malicious WAV/oversized endpoint output; audio-only installations/defaults and legacy endpoints; multiple mounted chat roots/canceled transcription never changes another draft.

Verification: 364 backend tests pass (39 audio cases); all eight browser suites pass, including real Chrome MediaRecorder/Web Audio with a synthetic microphone. Fresh broad review completed; all seven findings fixed, covering cancellation after decoder close, stale settings saves, fresh consent checks before capture, embedded editor navigation, environment URL changes, raw compressed response bounds, and browser capture timeout. Physical microphone and live transcription provider not checked.
