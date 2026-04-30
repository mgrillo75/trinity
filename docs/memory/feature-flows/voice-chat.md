# Voice Chat — Gemini 2.5 Flash Native Audio

**Status**: ✅ Phase 1 + Tool Calling Complete
**Date**: 2026-04-29
**Priority**: P1

---

## Problem Statement

Users need a fast, natural way to speak with agents via the browser. Text chat creates friction — typing is slow, reading long responses takes time, and the interaction feels robotic. A voice interface should feel like talking to a person: sub-500ms response time, natural turn-taking, interruption support.

---

## Core Concept

Use **Gemini 2.5 Flash Native Audio** (`gemini-live-2.5-flash-native-audio`) as a real-time voice proxy for the agent. Claude Code remains the agent's brain (handles complex tasks via tool calls), but the voice conversation runs on Gemini's speech-to-speech model for speed (~280ms TTFT). During voice sessions, Gemini can invoke a `run_task` function declaration to delegate work to the underlying Claude agent.

### Why Not Claude for Voice?

Anthropic has no speech-to-speech or realtime audio API. Any Claude voice pipeline would require STT → Claude text API (~500-800ms TTFT) → TTS, totaling 800ms-1.3s. Gemini's native audio model handles audio in/out natively with ~280ms latency and built-in turn-taking, barge-in, and emotion.

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                     Browser (Agent Detail)                  │
│                                                            │
│  ┌──────────────┐    ┌──────────────────────────────────┐  │
│  │  Chat Panel  │    │  VoiceOverlay.vue (canvas orb)   │  │
│  │  (existing)  │    │                                  │  │
│  │              │    │  Canvas orb — value noise + curl │  │
│  │  ... msgs    │    │  noise particles (220, 3 layers)  │  │
│  │              │    │  State hues: idle/connecting=0°   │  │
│  │              │    │  listening=+90° (green)           │  │
│  │              │    │  speaking=+210° (indigo)          │  │
│  │              │    │  tool_calling=amber badge overlay │  │
│  │              │    │  [Mute] [End Call]                │  │
│  └──────────────┘    └──────────────┬───────────────────┘  │
│                                     │                       │
│                    useVoiceSession.js composable            │
│                    (amplitude polling @ 30ms,               │
│                     tool_call / tool_result WS handlers)    │
│                                     │ WebSocket             │
└─────────────────────────────────────┼───────────────────────┘
                                      │
                                      ▼
                          ┌───────────────────────┐
                          │   Trinity Backend      │
                          │                        │
                          │  routers/voice.py      │
                          │  POST voice/start      │
                          │  WS /ws/voice/{id}     │
                          │                        │
                          │  services/gemini_voice │
                          │  VoiceSession          │
                          │  _execute_and_respond()│
                          │  (30s timeout)         │
                          │                        │
                          │  on_tool_call →        │
                          │    WS frame + audit log│
                          │  on_tool_result →      │
                          │    WS frame            │
                          └────────────┬───────────┘
                                       │
                          ┌────────────┴───────────┐
                          │                        │
                          ▼                        ▼
              ┌───────────────────┐   ┌────────────────────┐
              │  Gemini Live API  │   │  Agent Container   │
              │  (Vertex AI)      │   │  (Claude Code)     │
              │                   │   │                    │
              │  tools=[          │   │  agent_client      │
              │   run_task fn     │   │  .task(prompt)     │
              │  ]                │   │                    │
              └───────────────────┘   └────────────────────┘
```

---

## User Flow

### Starting a Voice Session

1. User is on Agent Detail page, Chat tab (authenticated)
2. User clicks **"Talk"** button (microphone icon) next to the chat input
3. `POST /api/agents/{name}/voice/start` with optional `voice_name`
4. Backend prepares the voice session:
   a. 3-level system prompt fallback: DB field → container file `voice-agent-system-prompt.md` → auto-generate from template info → generic
   b. Fetches recent chat history for context injection
5. Backend opens connection to Gemini Live API with `tools=[_RUN_TASK_TOOL]`
6. WebSocket bridge established: browser ↔ backend ↔ Gemini
7. Canvas orb overlay appears; state transitions drive hue rotation

### During the Voice Session

- User speaks; Gemini responds in real-time (~280ms TTFT)
- When Gemini calls `run_task`, backend dispatches `asyncio.create_task(_execute_and_respond())` (30s timeout)
- `tool_call` WS frame sent → orb shows amber badge; `tool_result` frame sent → orb returns to listening state
- All tool calls written to platform audit log
- Backend accumulates transcript from Gemini `serverContent` messages

### Ending a Voice Session

1. User clicks **"End"** button or closes overlay
2. Backend closes Gemini session, cancels any pending `_pending_tool_tasks`
3. Transcript saved as `ChatMessage` rows in existing `chat_messages` table
4. Chat panel refreshes showing voice conversation inline with text messages

---

## Requirements

### VOICE-001: Voice Session Initialization

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Backend endpoint | `POST /api/agents/{name}/voice/start` → returns `voice_session_id` + WebSocket URL |
| System prompt source | 3-level fallback: DB → `voice-agent-system-prompt.md` container file → auto-generate → generic |
| Context injection | Summarize last N messages of current chat session |
| Gemini connection | `google-genai` SDK, `generativelanguage.googleapis.com` Live API |
| Authentication | `GEMINI_API_KEY` platform setting |
| Audio format | PCM 16-bit, 16kHz mono |
| Voice name | Passed via `voice_name` field in `VoiceStartRequest` |

### VOICE-002: Audio Streaming Bridge

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Browser → Backend | WebSocket carrying raw audio frames from `getUserMedia()` |
| Backend → Gemini | Forward audio frames to Gemini Live API |
| Gemini → Backend | Receive audio response frames + transcript text |
| Backend → Browser | Forward audio frames for playback |
| Playback engine | AudioWorklet-first (blob URL inlined) with ScriptProcessor fallback |
| Amplitude | `createAudioPlayer()` exposes `getAmplitude()` (0–1 float) via `AnalyserNode` |

### VOICE-003: Transcript Persistence

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Transcript extraction | Captured from Gemini `serverContent` messages during session |
| Storage | Saved as `ChatMessage` rows in `chat_messages` table |
| Session linkage | Belongs to user's current `ChatSession` |
| Timing | Saved on session end |

### VOICE-004: Frontend Voice UI

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Trigger | Microphone icon button next to chat input textarea |
| Voice overlay | `VoiceOverlay.vue` — full canvas orb, pure JS (no CDN) |
| Particle system | Value noise + curl noise, 220 smoke particles in 3 layers, 9 pre-rendered sprite canvases |
| State hues | idle/connecting: 0°, listening: +90° (green), speaking: +210° (indigo), tool_calling: amber badge |
| Controls | Mute mic toggle, End call button |
| Amplitude polling | `setInterval(30ms)` via `amplitude` ref in composable |
| Audio capture | `navigator.mediaDevices.getUserMedia({ audio: true })` |
| Audio playback | AudioWorklet with ScriptProcessor fallback |

### VOICE-005: Voice System Prompt

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Lookup order | 1) DB field, 2) `voice-agent-system-prompt.md` in container, 3) auto-generate from template info, 4) generic fallback |
| Implementation | `_get_voice_system_prompt()` in `routers/voice.py` |

### VOICE-006: Conversation Summary for Context

**Status**: ✅ Implemented (truncation approach)

| Requirement | Detail |
|-------------|--------|
| Trigger | On voice session start |
| Method | Truncation of last N messages injected into system prompt |
| Fallback | Voice system prompt alone if no prior messages |

### VOICE-007: Tool Calling (run_task)

**Status**: ✅ Implemented (Phase 3 — shipped early)

| Requirement | Detail |
|-------------|--------|
| Function declaration | `_RUN_TASK_TOOL` (`FunctionDeclaration` for `run_task`) registered in `LiveConnectConfig` |
| Execution | `_execute_and_respond()` coroutine, `asyncio.create_task` per call, 30s `wait_for` timeout |
| Agent call | `agent_client.task(prompt)` (lazy import), truncated to `_TOOL_PROMPT_MAX=2000` chars |
| Error handling | `AgentNotReachableError` → "not currently running"; `AgentRequestError` → "Task error: ..." |
| Empty prompt | Falls back to "No prompt" |
| Session tracking | `_pending_tool_tasks` dict on `VoiceSession`; all cancelled on `end_session()` |
| WS events | `{type: "tool_call", tool_name: "run_task"}` and `{type: "tool_result", ...}` frames |
| Audit | Platform audit log written on each tool call via `on_tool_call` callback |

---

## WebSocket Message Types

**Client → Server:**
```json
{ "type": "audio", "data": "<base64 PCM audio>" }
```

**Server → Client:**
```json
{ "type": "audio", "data": "<base64 PCM audio>" }
{ "type": "transcript", "role": "user|assistant", "text": "..." }
{ "type": "status", "state": "listening|speaking|processing" }
{ "type": "tool_call", "tool_name": "run_task" }
{ "type": "tool_result", "tool_name": "run_task", "result": "..." }
```

---

## API Design

### POST /api/agents/{name}/voice/start

**Request:**
```json
{
  "session_id": "optional - existing chat session to continue",
  "voice_name": "optional - Gemini voice name"
}
```

**Response:**
```json
{
  "voice_session_id": "vs_abc123",
  "websocket_url": "wss://localhost:8000/ws/voice/vs_abc123",
  "chat_session_id": "cs_xyz789"
}
```

### POST /api/agents/{name}/voice/stop

**Response:**
```json
{
  "transcript": [...],
  "messages_saved": 12,
  "duration_seconds": 45,
  "cost": 0.003
}
```

---

## Configuration

### Platform-Level

| Setting | Description |
|---------|-------------|
| `GEMINI_API_KEY` | API key for Gemini Live API |
| `VOICE_ENABLED` | Global toggle |
| `VOICE_MODEL` | Model ID (default: `gemini-2.5-flash-native-audio-preview-12-2025`) |
| `VOICE_MAX_DURATION` | Max session duration in seconds (default: 300) |

### Per-Agent

| Setting | Description |
|---------|-------------|
| `voice_system_prompt` | Agent-specific voice personality prompt (DB field) |
| `voice_enabled` | Per-agent toggle |
| `voice_name` | Gemini voice selection (passed at session start) |

---

## Key Implementation Files

| Layer | File | Purpose |
|-------|------|---------|
| **Backend** | `src/backend/routers/voice.py` | Voice endpoints + WebSocket handler, `_get_voice_system_prompt()`, `on_tool_call`/`on_tool_result` callbacks |
| **Backend** | `src/backend/services/gemini_voice.py` | `VoiceSession`, `_RUN_TASK_TOOL` declaration, `_execute_tool()`, `_execute_and_respond()`, `_pending_tool_tasks` |
| **Frontend** | `src/frontend/src/components/chat/VoiceOverlay.vue` | Canvas orb (value noise + curl noise particles, state hues, amber tool badge) |
| **Frontend** | `src/frontend/src/composables/useVoiceSession.js` | Session state (`toolName`, `amplitude`, `isToolCalling`), WS message handlers, amplitude polling |
| **Frontend** | `src/frontend/src/utils/audio.js` | AudioWorklet-first capture/playback, `getAmplitude()` via `AnalyserNode` |
| **Tests** | `tests/unit/test_voice_tools.py` | 12 unit tests: `_execute_tool`, `_execute_and_respond`, tool declaration, session cancellation |

---

## Scope & Phasing

### Phase 1: MVP ✅ Complete

- Authenticated chat only (not public links)
- Single agent at a time
- Voice overlay with canvas orb visualization
- Transcript saved on session end
- 3-level voice system prompt fallback
- Gemini API key in platform settings
- `voice_name` selection at session start

### Phase 2: Polish (Partial)

- ✅ Real-time amplitude visualization (canvas orb driven by `getAmplitude()`)
- ⏳ Incremental transcript display in chat during session
- ⏳ Voice quality/latency metrics
- ⏳ Public link voice support

### Phase 3: Tool Calling ✅ Complete (shipped with Phase 1)

- ✅ `run_task` function calling (Gemini delegates to Claude agent mid-session)
- ✅ Amber badge UI state during tool execution
- ✅ 30s timeout with error recovery
- ⏳ Multi-language voice with auto-detection
- ⏳ Voice cloning / custom voice per agent

---

## Related Documentation

- [Authenticated Chat Tab](./authenticated-chat-tab.md) — Existing chat implementation
- [Persistent Chat Tracking](./persistent-chat-tracking.md) — Message storage
- [Gemini Runtime](./gemini-runtime.md) — Existing Gemini integration
- [Gemini Live API Docs](https://ai.google.dev/gemini-api/docs/live-api) — Official API reference
