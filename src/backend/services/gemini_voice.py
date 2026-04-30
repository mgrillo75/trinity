"""
Gemini Live API voice service for Trinity (VOICE-001).

Provides a wrapper around the google-genai SDK's Live API for real-time
speech-to-speech conversations with agents via Gemini 2.5 Flash Native Audio.

Architecture:
  Browser (mic) → WebSocket → Backend → Gemini Live API → Backend → WebSocket → Browser (speaker)
  Gemini tool_call → Backend._execute_tool → Agent container → tool_response → Gemini
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from google import genai
from google.genai import types as genai_types

from config import GEMINI_API_KEY, VOICE_MODEL, VOICE_MAX_DURATION

logger = logging.getLogger(__name__)

# Audio format constants
INPUT_SAMPLE_RATE = 16000   # 16kHz PCM input to Gemini
OUTPUT_SAMPLE_RATE = 24000  # 24kHz PCM output from Gemini

# Max chars for tool call prompts (prevent injection via very long args)
_TOOL_PROMPT_MAX = 2000

# Single tool declaration for all voice sessions
_RUN_TASK_TOOL = genai_types.Tool(
    function_declarations=[
        genai_types.FunctionDeclaration(
            name="run_task",
            description=(
                "Execute a task in the agent's workspace — look something up, "
                "read a file, search for information, or perform an action. "
                "Use this when you need live data or agent capabilities to answer accurately. "
                "Returns a text response from the agent."
            ),
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "prompt": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="Clear description of what to look up or do",
                    )
                },
                required=["prompt"],
            ),
        )
    ]
)


@dataclass
class VoiceTranscriptEntry:
    """A single transcript entry from the voice session."""
    role: str          # "user" or "assistant"
    text: str


@dataclass
class VoiceSession:
    """Tracks state for an active voice session."""
    session_id: str
    agent_name: str
    chat_session_id: str
    user_id: int
    user_email: str
    system_prompt: str
    voice_name: str = "Kore"
    transcript: list = field(default_factory=list)
    _gemini_session: object = field(default=None, repr=False)
    _send_task: object = field(default=None, repr=False)
    _receive_task: object = field(default=None, repr=False)
    _timeout_task: object = field(default=None, repr=False)
    _audio_in_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _pending_tool_tasks: dict = field(default_factory=dict)  # call_id → asyncio.Task
    _active: bool = False
    _duration_seconds: float = 0.0
    # Callbacks
    _on_audio_out: Optional[Callable] = field(default=None, repr=False)
    _on_transcript: Optional[Callable] = field(default=None, repr=False)
    _on_status: Optional[Callable] = field(default=None, repr=False)
    _on_tool_call: Optional[Callable] = field(default=None, repr=False)    # (name, args) → None
    _on_tool_result: Optional[Callable] = field(default=None, repr=False)  # (name, result) → None


class GeminiVoiceService:
    """Manages Gemini Live API voice sessions."""

    def __init__(self):
        self._client: Optional[genai.Client] = None
        self._sessions: dict[str, VoiceSession] = {}

    def is_available(self) -> bool:
        """Check if Gemini voice is configured."""
        return bool(GEMINI_API_KEY)

    def _get_client(self) -> genai.Client:
        """Get or create the Gemini client."""
        if not self._client:
            if not GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY not configured")
            self._client = genai.Client(api_key=GEMINI_API_KEY)
        return self._client

    def create_session(
        self,
        agent_name: str,
        chat_session_id: str,
        user_id: int,
        user_email: str,
        system_prompt: str,
        voice_name: str = "Kore",
    ) -> VoiceSession:
        """Create a new voice session (does not connect yet)."""
        session_id = f"vs_{secrets.token_urlsafe(16)}"
        session = VoiceSession(
            session_id=session_id,
            agent_name=agent_name,
            chat_session_id=chat_session_id,
            user_id=user_id,
            user_email=user_email,
            system_prompt=system_prompt,
            voice_name=voice_name,
        )
        self._sessions[session_id] = session
        logger.info(f"Voice session created: {session_id} for agent {agent_name}")
        return session

    async def connect_and_stream(
        self,
        session_id: str,
        on_audio_out: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]],  # (role, text)
        on_status: Callable[[str], Awaitable[None]],           # status string
        on_tool_call: Optional[Callable] = None,               # (name, args) → None
        on_tool_result: Optional[Callable] = None,             # (name, result) → None
    ):
        """
        Connect to Gemini Live API and begin streaming.

        This is the main loop that runs for the lifetime of the voice session.
        It spawns send/receive tasks and waits until the session ends.
        Tool calls are executed asynchronously against the agent container.
        """
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Voice session {session_id} not found")

        session._on_audio_out = on_audio_out
        session._on_transcript = on_transcript
        session._on_status = on_status
        session._on_tool_call = on_tool_call
        session._on_tool_result = on_tool_result
        session._active = True

        client = self._get_client()

        config = genai_types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=session.system_prompt,
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=session.voice_name
                    )
                )
            ),
            tools=[_RUN_TASK_TOOL],
        )

        try:
            await on_status("connecting")

            async with client.aio.live.connect(
                model=VOICE_MODEL,
                config=config,
            ) as gemini_session:
                session._gemini_session = gemini_session
                await on_status("listening")

                # Run send and receive concurrently with a timeout
                async with asyncio.TaskGroup() as tg:
                    session._send_task = tg.create_task(
                        self._send_audio_loop(session)
                    )
                    session._receive_task = tg.create_task(
                        self._receive_audio_loop(session)
                    )
                    session._timeout_task = tg.create_task(
                        self._timeout_watchdog(session)
                    )

        except* asyncio.CancelledError:
            logger.info(f"Voice session {session_id} cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error(f"Voice session {session_id} error: {exc}")
        finally:
            session._active = False
            await on_status("ended")
            logger.info(f"Voice session {session_id} ended, transcript entries: {len(session.transcript)}")

    async def _send_audio_loop(self, session: VoiceSession):
        """Forward audio from the input queue to Gemini."""
        while session._active:
            try:
                chunk = await asyncio.wait_for(
                    session._audio_in_queue.get(), timeout=1.0
                )
                if chunk is None:
                    # Poison pill — stop sending
                    break
                await session._gemini_session.send_realtime_input(
                    audio={"data": chunk, "mime_type": f"audio/pcm;rate={INPUT_SAMPLE_RATE}"}
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Send audio error: {e}")
                break

    async def _receive_audio_loop(self, session: VoiceSession):
        """Receive audio, transcriptions, and tool calls from Gemini."""
        current_user_text = ""
        current_assistant_text = ""

        while session._active:
            try:
                turn = session._gemini_session.receive()
                async for response in turn:
                    if not session._active:
                        return

                    # Tool calls — spawn async task per call, keyed by call_id
                    if hasattr(response, 'tool_call') and response.tool_call:
                        fc_list = getattr(response.tool_call, 'function_calls', []) or []
                        for fc in fc_list:
                            call_id = getattr(fc, 'id', None) or secrets.token_hex(8)
                            task = asyncio.create_task(
                                self._execute_and_respond(session, call_id, fc)
                            )
                            session._pending_tool_tasks[call_id] = task
                        continue

                    content = response.server_content
                    if not content:
                        continue

                    # Audio output
                    if content.model_turn:
                        if session._on_status:
                            await session._on_status("speaking")
                        for part in content.model_turn.parts:
                            if part.inline_data and isinstance(part.inline_data.data, bytes):
                                if session._on_audio_out:
                                    await session._on_audio_out(part.inline_data.data)

                    # Input transcription (what the user said)
                    if hasattr(content, 'input_transcription') and content.input_transcription:
                        text = content.input_transcription.text
                        if text and text.strip():
                            current_user_text += text
                            if session._on_transcript:
                                await session._on_transcript("user", text)

                    # Output transcription (what Gemini said)
                    if hasattr(content, 'output_transcription') and content.output_transcription:
                        text = content.output_transcription.text
                        if text and text.strip():
                            current_assistant_text += text
                            if session._on_transcript:
                                await session._on_transcript("assistant", text)

                    # Turn complete
                    if content.turn_complete:
                        if session._on_status:
                            await session._on_status("listening")

                        if current_user_text.strip():
                            session.transcript.append(
                                VoiceTranscriptEntry(role="user", text=current_user_text.strip())
                            )
                            current_user_text = ""
                        if current_assistant_text.strip():
                            session.transcript.append(
                                VoiceTranscriptEntry(role="assistant", text=current_assistant_text.strip())
                            )
                            current_assistant_text = ""

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if session._active:
                    logger.error(f"Receive audio error: {e}")
                break

        # Flush any remaining text
        if current_user_text.strip():
            session.transcript.append(
                VoiceTranscriptEntry(role="user", text=current_user_text.strip())
            )
        if current_assistant_text.strip():
            session.transcript.append(
                VoiceTranscriptEntry(role="assistant", text=current_assistant_text.strip())
            )

    async def _execute_and_respond(self, session: VoiceSession, call_id: str, fc):
        """Execute a Gemini tool call and send the response back. Runs as a background task."""
        tool_name = getattr(fc, 'name', 'run_task')
        args = dict(fc.args) if getattr(fc, 'args', None) else {}

        try:
            if session._on_tool_call:
                await session._on_tool_call(tool_name, args)

            result = await asyncio.wait_for(
                self._execute_tool(session.agent_name, tool_name, args),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            result = "Tool execution timed out after 30 seconds."
            logger.warning(f"Voice tool call timed out: {tool_name} session={session.session_id}")
        except Exception as e:
            result = f"Tool error: {str(e)[:200]}"
            logger.error(f"Voice tool call error: {e}")
        finally:
            session._pending_tool_tasks.pop(call_id, None)

        if session._on_tool_result:
            try:
                await session._on_tool_result(tool_name, result)
            except Exception:
                pass

        if session._gemini_session and session._active:
            try:
                await session._gemini_session.send_tool_response(
                    function_responses=[
                        genai_types.FunctionResponse(
                            id=call_id,
                            name=tool_name,
                            response={"output": result},
                        )
                    ]
                )
            except Exception as e:
                logger.error(f"Failed to send tool response for {call_id}: {e}")

    async def _execute_tool(self, agent_name: str, tool_name: str, args: dict) -> str:
        """Route a tool call to the agent container via the task endpoint."""
        from services.agent_client import get_agent_client, AgentNotReachableError, AgentRequestError

        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "No prompt provided."
        if len(prompt) > _TOOL_PROMPT_MAX:
            prompt = prompt[:_TOOL_PROMPT_MAX] + "..."

        logger.info(f"Voice tool call: agent={agent_name} tool={tool_name} prompt={prompt[:80]!r}")
        try:
            client = get_agent_client(agent_name)
            response = await client.task(prompt, timeout=28.0)
            return response.response or "Task completed with no response."
        except AgentNotReachableError:
            return f"Agent {agent_name!r} is not currently running."
        except AgentRequestError as e:
            return f"Task error: {str(e)[:200]}"
        except Exception as e:
            logger.error(f"Voice tool execution error for {agent_name}: {e}")
            return f"Execution error: {str(e)[:200]}"

    async def _timeout_watchdog(self, session: VoiceSession):
        """Auto-end session after max duration."""
        await asyncio.sleep(VOICE_MAX_DURATION)
        if session._active:
            logger.info(f"Voice session {session.session_id} hit max duration ({VOICE_MAX_DURATION}s)")
            await self.end_session(session.session_id)

    async def send_audio(self, session_id: str, audio_data: bytes):
        """Queue audio data for sending to Gemini."""
        session = self._sessions.get(session_id)
        if session and session._active:
            await session._audio_in_queue.put(audio_data)

    async def end_session(self, session_id: str) -> Optional[VoiceSession]:
        """End a voice session and return it with transcript."""
        session = self._sessions.get(session_id)
        if not session:
            return None

        session._active = False

        # Send poison pill to unblock send loop
        await session._audio_in_queue.put(None)

        # Cancel pending tool tasks
        for task in list(session._pending_tool_tasks.values()):
            if not task.done():
                task.cancel()
        session._pending_tool_tasks.clear()

        # Cancel send/receive/timeout tasks
        for task in [session._send_task, session._receive_task, session._timeout_task]:
            if task and not task.done():
                task.cancel()

        logger.info(f"Voice session {session_id} ended")
        return session

    def get_session(self, session_id: str) -> Optional[VoiceSession]:
        """Get a voice session by ID."""
        return self._sessions.get(session_id)

    def remove_session(self, session_id: str):
        """Remove a session from tracking."""
        self._sessions.pop(session_id, None)


# Singleton
voice_service = GeminiVoiceService()
