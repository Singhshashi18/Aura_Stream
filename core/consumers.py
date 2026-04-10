import asyncio
import base64
import json
import math
import os
import re
import struct
import time
from contextlib import suppress

import websockets
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .models import AuraSession, ThoughtLog


def _resolve_realtime_url(model: str) -> str:
    explicit = os.getenv("OPENAI_REALTIME_URL", "").strip()
    if explicit:
        return explicit.format(model=model) if "{model}" in explicit else explicit

    return f"wss://api.openai.com/v1/realtime?model={model}"


def _rms_int16(audio_bytes: bytes) -> float:
    if len(audio_bytes) < 2:
        return 0.0
    sample_count = len(audio_bytes) // 2
    samples = struct.unpack("<" + "h" * sample_count, audio_bytes[: sample_count * 2])
    energy = sum(float(s) * float(s) for s in samples) / sample_count
    return math.sqrt(energy)


def _looks_like_json(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        return True
    return bool(re.search(r'"\s*:\s*', s))


def _is_probably_english(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return True

    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return True

    # Fast filter for scripts that are unlikely to be English.
    ascii_letters = [ch for ch in letters if ord(ch) < 128]
    if (len(ascii_letters) / len(letters)) < 0.9:
        return False

    # English lexical signal for latin-script text.
    words = re.findall(r"[a-z']+", s)
    if len(words) < 3:
        return True

    english_markers = {
        "the", "and", "is", "are", "to", "of", "in", "for", "with", "that", "this",
        "you", "your", "it", "on", "as", "be", "can", "do", "what", "which", "how",
        "i", "we", "they", "a", "an", "or", "if", "from", "about", "please",
    }
    hits = sum(1 for w in words if w in english_markers)
    return (hits / len(words)) >= 0.12


def _normalize_spoken_text(text: str) -> str:
    if not text:
        return ""
    out = re.sub(r"\s+", " ", text).strip()
    if _looks_like_json(out):
        return ""
    if len(out) > 420:
        out = out[:420].rstrip()
        if "." in out:
            out = out[: out.rfind(".") + 1]
    return out


def _fallback_response(user_hint: str) -> str:
    if user_hint:
        short_hint = user_hint[:120].strip()
        return f"I heard your request: {short_hint}. Please repeat it in one short sentence so I can answer accurately."
    return "I want to answer correctly. Please repeat your request in one short sentence."


def _wants_english_only(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    markers = [
        "english only",
        "respond in english",
        "speak english",
        "use english",
        "only english",
    ]
    return any(m in s for m in markers)


def _english_recovery_response(user_hint: str) -> str:
    if user_hint:
        short_hint = user_hint[:120].strip()
        return f"I will reply in English only. Based on your request: {short_hint}. Please continue, and I will answer in English."
    return "I will reply in English only. Please repeat your request, and I will answer in English."


def _has_non_english_cues(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    # Common cues that frequently indicate non-English output.
    cues = [
        "hola", "gracias", "por favor", "bonjour", "merci", "s'il", "oui", "non",
        "hindi", "namaste", "dhanyavad", "usted", "estoy", "como", "porque",
    ]
    return any(c in s for c in cues)


def _estimate_rms_variance(rms_window: list[float]) -> float:
    """Calculate variance of RMS values over time.
    
    High variance = speech (modulating energy)
    Low variance = background noise (steady energy)
    """
    if len(rms_window) < 2:
        return 0.0
    mean_rms = sum(rms_window) / len(rms_window)
    variance = sum((x - mean_rms) ** 2 for x in rms_window) / len(rms_window)
    return variance


def _is_likely_speech(rms_value: float, rms_window: list[float], rms_threshold: float = 420.0) -> tuple[bool, str]:
    """Distinguish between user speech and background noise.
    
    Returns (is_speech: bool, detection_type: str)
    Where detection_type is "user_speech", "background_noise", or "silence"
    
    Strategy:
    - If RMS is below threshold → silence
    - If RMS is above threshold:
        - Calculate RMS variance from window (speech has high variance, doorbell has low)
        - If variance is high with good energy variation → user_speech
        - If variance is low (steady tone) → likely background_noise
    """
    if rms_value < rms_threshold:
        return (False, "silence")
    
    # If we have enough history, check if this is steady noise or modulating speech
    if len(rms_window) >= 5:
        variance = _estimate_rms_variance(rms_window[-5:])
        
        # Speech typically has variance > 500 (modulating energy)
        # Doorbell/alert tones are very steady (variance < 50)
        if variance < 50:  # Very steady tone = background noise (doorbell)
            return (False, "background_noise")
        
        # Additional check: Look at energy gradient (how much RMS changes between chunks)
        if len(rms_window) >= 2:
            recent_deltas = [abs(rms_window[i] - rms_window[i-1]) for i in range(1, min(len(rms_window), 5))]
            avg_gradient = sum(recent_deltas) / len(recent_deltas) if recent_deltas else 0
            
            # If both variance is very low AND gradient is minimal, it's definitely background noise
            if variance < 100 and avg_gradient < 30:
                return (False, "background_noise")
    
    return (True, "user_speech")


class CallStreamConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.chunk_count = 0
        self.total_bytes = 0
        self.speech_chunks = 0
        self.silence_chunks = 0
        self.vad_threshold = 420.0
        self.session_uuid = None
        self.prompt = ""
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-realtime-preview")
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_ws = None
        self.openai_reader_task = None
        self.keep_open = True
        self.shutting_down = False
        self.response_buffer = []
        self.audio_buffer = bytearray()
        self.local_voice_active = False
        self.local_silence_run = 0
        self.awaiting_response = False
        self.assistant_output_active = False
        self.response_in_progress = False
        self.cancel_requested = False
        self.last_response_request_at = 0.0
        self.response_request_cooldown_sec = 0.8
        self.speech_interrupt_streak = 0
        self.last_server_cancel_at = 0.0
        self.server_cancel_cooldown_sec = 1.2
        self.last_user_transcript = ""
        self.recent_turns = []
        self.english_only_preference = True
        self.language_retry_pending = False
        self.realtime_blocked = False
        
        # New: Background noise detection and response resumption
        self.rms_history = []  # Track RMS values for variance analysis (max 10 samples)
        self.interrupted_response = ""  # Store partial response when interrupted
        self.interruption_type = "not_interrupted"  # Track if user speech vs background noise
        self.was_recently_interrupted = False  # Flag to inject resumption context in next response

        await self.accept()
        await self.send(text_data=json.dumps({"event": "connected", "message": "Call stream socket connected"}))

    @database_sync_to_async
    def _create_session(self) -> str:
        session = AuraSession.objects.create(model_type=self.model)
        return str(session.uuid)

    @database_sync_to_async
    def _store_thought(self, thought: str, final_response: str, interrupted_by: str = "", interruption_type: str = "not_interrupted") -> None:
        if not self.session_uuid:
            return
        session = AuraSession.objects.get(uuid=self.session_uuid)
        ThoughtLog.objects.create(
            session=session,
            thought_block=thought,
            final_response=final_response,
            interrupted_by=interrupted_by,
            interruption_type=interruption_type,
        )

    @database_sync_to_async
    def _get_recent_memory(self, limit: int = 5) -> list[dict]:
        if not self.session_uuid:
            return []
        rows = (
            ThoughtLog.objects.filter(session__uuid=self.session_uuid)
            .order_by("-id")
            .values_list("thought_block", "final_response")[:limit]
        )
        cleaned = []
        for thought, response in rows:
            t = str(thought or "").strip()
            r = str(response or "").strip()
            if t or r:
                cleaned.append({"user": t, "assistant": r})
        cleaned.reverse()
        return cleaned

    async def _build_response_instructions(self) -> str:
        db_memory = await self._get_recent_memory(limit=5)
        memory_chunks = [f"- assistant: {(m.get('assistant') or '')[:180]}" for m in db_memory if (m.get("assistant") or "").strip()]
        user_detail_chunks = [f"- user detail: {(m.get('user') or '')[:180]}" for m in db_memory if (m.get("user") or "").strip()]

        # Keep a lightweight local memory of this live call, including user transcript snippets.
        local_turns = []
        for turn in self.recent_turns[-2:]:
            user = (turn.get("user") or "").strip()
            assistant = (turn.get("assistant") or "").strip()
            if user or assistant:
                local_turns.append(f"- user: {user[:120]} | assistant: {assistant[:140]}")

        latest_user = (self.last_user_transcript or "").strip()

        instructions_parts = [
            "CRITICAL: Respond in English only.",
            "Never use any language other than English.",
            "Answer the user's latest request directly and accurately.",
            "If the request is ambiguous, ask one short clarifying question.",
            "Do not output JSON, objects, key-value format, or markdown lists.",
            "Always speak naturally in concise sentences unless the user asks for detail.",
        ]

        if self.english_only_preference:
            instructions_parts.append("Sticky preference: The user asked for English-only responses. Keep all replies in English.")
        if self.language_retry_pending:
            instructions_parts.append("Previous response drifted from English. Retry now in clear, simple English only.")

        if self.prompt:
            instructions_parts.append(f"Session guidance: {self.prompt}")
        if latest_user:
            instructions_parts.append(f"Latest user request: {latest_user[:200]}")
            instructions_parts.append(f"LANGUAGE: Respond to '{latest_user[:100]}' in ENGLISH.")
        
        # Add resumption context if we were interrupted mid-response
        if self.was_recently_interrupted and self.interrupted_response:
            resumption_note = (
                f"INTERRUPTION: I was saying '{self.interrupted_response[:150]}...' but you interrupted me. "
                f"Your new question is: '{latest_user[:100]}'. "
                f"PRIORITY: Answer the new question directly and completely. Keep the context of what I was saying in mind, but focus on answering the new question accurately."
            )
            instructions_parts.append(resumption_note)
            self.was_recently_interrupted = False  # Reset flag after using it
        
        if memory_chunks:
            instructions_parts.append("Recent assistant memory:\n" + "\n".join(memory_chunks))
        if user_detail_chunks:
            instructions_parts.append("User-provided details to remember:\n" + "\n".join(user_detail_chunks))
        if local_turns:
            instructions_parts.append("Recent live turns:\n" + "\n".join(local_turns))

        return "\n\n".join(instructions_parts)

    async def _open_openai_realtime(self):
        if self.openai_ws or not self.api_key:
            return

        url = _resolve_realtime_url(self.model)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        try:
            self.openai_ws = await websockets.connect(url, additional_headers=headers)
        except Exception as exc:
            self.openai_ws = None
            await self.send(text_data=json.dumps({"event": "error", "message": f"Failed to connect to OpenAI Realtime: {exc}"}))
            return

        session_payload = {
            "model": self.model,
            "output_modalities": ["text", "audio"],
            "instructions": (
                "SYSTEM LANGUAGE POLICY (NON-OVERRIDABLE): Respond in English only from the very first reply. "
                + "Never output another language unless the user explicitly asks to switch language for this session. "
                + "If uncertain, still answer in simple English. "
                + (self.prompt + "\n\n" if self.prompt else "")
                + "You are Aura-Stream, a helpful voice assistant. "
                + "If the user interrupts, stop speaking immediately. "
                + "If the user says stop, remain silent after acknowledging once."
            ),
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": 16000,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": False,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": 16000,
                    },
                    "voice": "marin",
                },
            },
            "temperature": 0.2,
        }

        session_update = {
            "type": "session.update",
            "session": session_payload,
        }
        await self.openai_ws.send(json.dumps(session_update))
        self.openai_reader_task = asyncio.create_task(self._read_openai_messages())
        await self.send(text_data=json.dumps({"event": "realtime_ready", "model": self.model}))

    async def _close_openai_realtime(self):
        self.keep_open = False
        if self.openai_reader_task:
            self.openai_reader_task.cancel()
            with suppress(Exception):
                await asyncio.wait_for(self.openai_reader_task, timeout=0.8)
            self.openai_reader_task = None
        if self.openai_ws:
            try:
                with suppress(Exception):
                    await asyncio.wait_for(self.openai_ws.close(), timeout=0.6)
            finally:
                self.openai_ws = None

    async def _forward_audio(self, audio_bytes: bytes):
        if not self.openai_ws:
            return
        payload = base64.b64encode(audio_bytes).decode("ascii")
        await self.openai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))

    async def _request_response(self):
        now = time.monotonic()
        if (
            not self.openai_ws
            or self.awaiting_response
            or self.response_in_progress
            or self.cancel_requested
            or (now - self.last_response_request_at) < self.response_request_cooldown_sec
        ):
            return
        self.awaiting_response = True
        self.response_in_progress = True
        self.last_response_request_at = now
        dynamic_instructions = await self._build_response_instructions()
        # Always enforce English in each response to prevent language switching
        enforced_instructions = (
            "SYSTEM LANGUAGE POLICY (NON-OVERRIDABLE): Respond in English only. "
            + "Do not switch language unless user explicitly asks to switch language. "
            + dynamic_instructions
        )
        await self.openai_ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "voice": "marin",
                        "instructions": enforced_instructions,
                    },
                }
            )
        )

    async def _read_openai_messages(self):
        try:
            async for raw in self.openai_ws:
                event = json.loads(raw)
                event_type = event.get("type", "unknown")

                if event_type == "input_audio_buffer.speech_started":
                    await self.send(text_data=json.dumps({"event": "speech_started"}))
                    if self.assistant_output_active and self.openai_ws and not self.cancel_requested:
                        # Capture what the assistant was saying before interruption
                        self.interrupted_response = "".join(self.response_buffer).strip()
                        self.was_recently_interrupted = True  # Flag for next response resumption
                        
                        with suppress(Exception):
                            await self.openai_ws.send(json.dumps({"type": "response.cancel"}))
                        self.cancel_requested = True
                        self.awaiting_response = False
                        self.assistant_output_active = False
                        await self.send(text_data=json.dumps({"event": "barge_in", "message": "Assistant response cancelled", "was_saying": self.interrupted_response[:100]}))
                elif event_type == "input_audio_buffer.speech_stopped":
                    await self.send(text_data=json.dumps({"event": "speech_stopped"}))
                    await self._request_response()
                elif event_type == "response.created":
                    self.response_in_progress = True
                    self.awaiting_response = False
                    self.assistant_output_active = True
                    self.cancel_requested = False
                    self.language_retry_pending = False
                    await self.send(text_data=json.dumps({"event": "response_started"}))
                elif event_type in {"response.output_audio.delta", "response.audio.delta"}:
                    delta = event.get("delta", "")
                    if delta and not self.local_voice_active:
                        await self.send(text_data=json.dumps({"event": "assistant_audio_delta", "audio": delta}))
                elif event_type in {"response.output_text.delta", "response.text.delta"}:
                    delta = event.get("delta", "")
                    if delta:
                        self.response_buffer.append(delta)
                        await self.send(text_data=json.dumps({"event": "assistant_text_delta", "text": delta}))
                elif event_type == "response.output_audio_transcript.delta":
                    delta = event.get("delta", "")
                    if delta:
                        self.response_buffer.append(delta)
                        await self.send(text_data=json.dumps({"event": "assistant_text_delta", "text": delta}))
                        # Hard English lock: if transcript drifts to non-English, cancel and retry in English.
                        partial = _normalize_spoken_text("".join(self.response_buffer).strip())
                        if (
                            self.english_only_preference
                            and len(partial) >= 28
                            and (_has_non_english_cues(partial) or not _is_probably_english(partial))
                            and self.openai_ws
                            and not self.cancel_requested
                        ):
                            with suppress(Exception):
                                await self.openai_ws.send(json.dumps({"type": "response.cancel"}))
                            self.cancel_requested = True
                            self.language_retry_pending = True
                            self.assistant_output_active = False
                            self.awaiting_response = False
                            self.response_in_progress = False
                            self.response_buffer = []
                            await self.send(text_data=json.dumps({"event": "barge_in", "message": "Regenerating in English only"}))
                            await self._request_response()
                elif event_type == "response.output_audio_transcript.done":
                    transcript = event.get("transcript", "")
                    if transcript and not "".join(self.response_buffer).strip():
                        self.response_buffer.append(transcript)
                        await self.send(text_data=json.dumps({"event": "assistant_text_delta", "text": transcript}))
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = (event.get("transcript") or "").strip()
                    if transcript:
                        self.last_user_transcript = transcript
                        if _wants_english_only(transcript):
                            self.english_only_preference = True
                        await self.send(text_data=json.dumps({"event": "user_transcript", "text": transcript}))
                elif event_type == "response.done":
                    final_text = _normalize_spoken_text("".join(self.response_buffer).strip())
                    if not final_text or not _is_probably_english(final_text):
                        if self.english_only_preference:
                            final_text = _english_recovery_response(self.last_user_transcript)
                            self.language_retry_pending = False
                        else:
                            final_text = _fallback_response(self.last_user_transcript)

                    self.recent_turns.append({"user": self.last_user_transcript, "assistant": final_text})
                    if len(self.recent_turns) > 6:
                        self.recent_turns = self.recent_turns[-6:]

                    # Store thought with interruption context
                    await self._store_thought(
                        self.last_user_transcript,
                        final_text,
                        interrupted_by=self.interrupted_response,
                        interruption_type=self.interruption_type,
                    )
                    
                    # Reset interruption tracking
                    self.interrupted_response = ""
                    self.interruption_type = "not_interrupted"
                    
                    self.response_buffer = []
                    self.awaiting_response = False
                    self.assistant_output_active = False
                    self.response_in_progress = False
                    self.cancel_requested = False
                    self.language_retry_pending = False
                    await self.send(text_data=json.dumps({"event": "assistant_done", "text": final_text}))
                elif event_type == "error":
                    err = event.get("error", {})
                    code = err.get("code", "")
                    await self.send(
                        text_data=json.dumps(
                            {
                                "event": "error",
                                "message": err.get("message", event.get("message", "Realtime error")),
                                "details": event,
                            }
                        )
                    )
                    if code == "insufficient_quota":
                        self.realtime_blocked = True
                        self.awaiting_response = False
                        self.assistant_output_active = False
                        self.response_in_progress = False
                        self.cancel_requested = False
                        self.language_retry_pending = False
                        await self.send(
                            text_data=json.dumps(
                                {
                                    "event": "quota_exceeded",
                                    "message": "OpenAI quota exceeded. Update billing/plan, then reconnect.",
                                }
                            )
                        )
                        if self.openai_ws:
                            with suppress(Exception):
                                await self.openai_ws.close()
                            self.openai_ws = None
                    elif code == "conversation_already_has_active_response":
                        self.response_in_progress = True
                        self.awaiting_response = False
                    elif code == "response_cancel_not_active":
                        self.cancel_requested = False
                        self.awaiting_response = False
                        self.response_in_progress = False
                        self.language_retry_pending = False
                    else:
                        self.awaiting_response = False
                        self.assistant_output_active = False
                        self.response_in_progress = False
                        self.cancel_requested = False
                        self.language_retry_pending = False
                else:
                    if event_type in {"session.created", "session.updated", "rate_limits.updated"}:
                        await self.send(text_data=json.dumps({"event": "openai_event", "type": event_type}))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self.send(text_data=json.dumps({"event": "error", "message": f"Realtime reader failed: {exc}"}))

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data:
            if self.realtime_blocked:
                return

            self.chunk_count += 1
            self.total_bytes += len(bytes_data)

            rms = _rms_int16(bytes_data)
            
            # Track RMS history for variance-based speech/noise detection
            self.rms_history.append(rms)
            if len(self.rms_history) > 10:
                self.rms_history.pop(0)
            
            # Smart speech vs noise detection
            is_speech, detection_type = _is_likely_speech(rms, self.rms_history, self.vad_threshold)
            
            if is_speech:
                self.speech_chunks += 1
                self.audio_buffer.extend(bytes_data)
                self.local_voice_active = True
                self.local_silence_run = 0
                self.speech_interrupt_streak += 1
                self.interruption_type = "user_speech"  # Mark as user speech for DB logging
                
                # Only interrupt if this is actual user speech, not background noise
                now = time.monotonic()
                if (
                    (self.awaiting_response or self.assistant_output_active)
                    and self.openai_ws
                    and rms >= (self.vad_threshold + 350)
                    and self.speech_interrupt_streak >= 3
                    and (now - self.last_server_cancel_at) >= self.server_cancel_cooldown_sec
                    and not self.cancel_requested
                ):
                    await self.openai_ws.send(json.dumps({"type": "response.cancel"}))
                    self.last_server_cancel_at = now
                    self.cancel_requested = True
                    self.awaiting_response = False
                    self.assistant_output_active = False
                    self.response_in_progress = True
                    await self.send(text_data=json.dumps({"event": "barge_in", "message": "Assistant interrupted by user speech", "detection": "user_speech"}))
            elif detection_type == "background_noise":
                # Ignore background noise - don't treat it as interruption
                self.silence_chunks += 1
                self.speech_interrupt_streak = 0
                # Don't cancel response for background noise
                await self.send(text_data=json.dumps({"event": "background_noise_detected"}))
            else:  # silence
                self.silence_chunks += 1
                self.speech_interrupt_streak = 0
                if self.local_voice_active:
                    self.local_silence_run += 1
                    if self.local_silence_run >= 2:
                        self.local_voice_active = False

            if self.openai_ws:
                await self._forward_audio(bytes_data)

            # Local fallback VAD gate: trigger a response after short silence following speech.
            if (
                self.openai_ws
                and self.local_voice_active
                and self.local_silence_run >= 6
                and not self.awaiting_response
                and not self.response_in_progress
            ):
                self.local_voice_active = False
                self.local_silence_run = 0
                await self._request_response()
                await self.send(text_data=json.dumps({"event": "local_vad_commit"}))

            duration_seconds = self.total_bytes / (2 * 16000)
            if self.chunk_count % 8 == 0:
                await self.send(
                    text_data=json.dumps(
                        {
                            "event": "buffer_update",
                            "chunks": self.chunk_count,
                            "bytes": self.total_bytes,
                            "duration_seconds": round(duration_seconds, 3),
                            "speech_chunks": self.speech_chunks,
                            "silence_chunks": self.silence_chunks,
                            "vad": {"rms": round(rms, 2), "speech": is_speech, "detection": detection_type},
                        }
                    )
                )
            return

        if not text_data:
            return

        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({"event": "error", "message": "Invalid JSON message"}))
            return

        event = payload.get("event")
        if event == "start":
            self.prompt = payload.get("prompt", "")
            self.session_uuid = await self._create_session()
            await self._open_openai_realtime()
            if not self.openai_ws:
                return
            await self.send(
                text_data=json.dumps(
                    {
                        "event": "started",
                        "sample_rate": payload.get("sample_rate", 16000),
                        "channels": payload.get("channels", 1),
                        "session_uuid": self.session_uuid,
                        "model": self.model,
                    }
                )
            )
            return

        if event == "stop":
            if self.openai_ws and self.local_voice_active and not self.awaiting_response and not self.response_in_progress:
                self.local_voice_active = False
                self.local_silence_run = 0
                await self._request_response()
            await self.send(
                text_data=json.dumps(
                    {
                        "event": "stopped",
                        "chunks": self.chunk_count,
                        "bytes": self.total_bytes,
                        "speech_chunks": self.speech_chunks,
                        "silence_chunks": self.silence_chunks,
                    }
                )
            )
            return

        if event == "interrupt":
            if self.openai_ws and (self.awaiting_response or self.assistant_output_active):
                with suppress(Exception):
                    await self.openai_ws.send(json.dumps({"type": "response.cancel"}))
                self.cancel_requested = True
                self.awaiting_response = False
                self.assistant_output_active = False
                self.response_in_progress = True
                await self.send(text_data=json.dumps({"event": "barge_in", "message": "Assistant interrupted by client"}))
            return

        await self.send(text_data=json.dumps({"event": "echo", "payload": payload}))

    async def disconnect(self, close_code):
        if self.shutting_down:
            return
        self.shutting_down = True
        self.keep_open = False
        self.audio_buffer.clear()
        await self._close_openai_realtime()
