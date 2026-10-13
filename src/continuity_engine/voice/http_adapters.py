"""Opt-in OpenAI-compatible HTTP ASR/TTS adapters.

Audio capture/playback are host-owned. This module never opens a microphone or audio
output device at import time. ASR uploads the supplied segment only for the request;
TTS audio is handed to an explicitly supplied sink and is not written to disk.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse


class VoiceProviderConfigurationError(ValueError):
    pass


class VoiceProviderResponseError(RuntimeError):
    pass


def _validate_endpoint(endpoint_url: str) -> None:
    parsed = urlparse(endpoint_url) if isinstance(endpoint_url, str) else None
    if (parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username is not None or parsed.password is not None or not parsed.path):
        raise VoiceProviderConfigurationError("endpoint_url must be an explicit HTTP(S) URL without embedded credentials")
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise VoiceProviderConfigurationError("plain HTTP is permitted only for loopback endpoints")


def _httpx():
    try:
        import httpx
        return httpx
    except ImportError as exc:
        raise RuntimeError("Install provider dependencies: pip install 'continuity-engine[provider]'") from exc


class OpenAICompatibleSpeechRecognizer:
    """Client for an OpenAI-compatible `/audio/transcriptions` endpoint.

    Input should be a valid WAV/MP3/WebM etc. segment supported by the provider. Arbitrary
    PCM frames should be wrapped in a correct container by the host before submission.
    """
    def __init__(self, *, endpoint_url: str, model: str, api_key: str | None = None,
                 timeout_s: float = 30.0, language: str | None = None,
                 filename: str = "audio.wav", content_type: str = "audio/wav",
                 client: Any | None = None, max_audio_bytes: int = 16_000_000):
        _validate_endpoint(endpoint_url)
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise VoiceProviderConfigurationError("model must be a non-empty string <= 200 chars")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise VoiceProviderConfigurationError("api_key must be omitted or non-empty")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(float(timeout_s)) or timeout_s <= 0:
            raise VoiceProviderConfigurationError("timeout_s must be finite and positive")
        if not filename or len(filename) > 100 or "/" in filename or "\\" in filename:
            raise VoiceProviderConfigurationError("filename must be a safe basename")
        if not isinstance(content_type, str) or "/" not in content_type or len(content_type) > 100:
            raise VoiceProviderConfigurationError("content_type is invalid")
        if isinstance(max_audio_bytes, bool) or not isinstance(max_audio_bytes, int) or not 1024 <= max_audio_bytes <= 64_000_000:
            raise VoiceProviderConfigurationError("max_audio_bytes must be in [1024,64000000]")
        self.endpoint_url, self.model, self.api_key = endpoint_url, model.strip(), api_key
        self.timeout_s, self.language = float(timeout_s), language
        self.filename, self.content_type = filename, content_type
        self.max_audio_bytes = max_audio_bytes
        self._owns_client = client is None
        self._client = client or _httpx().AsyncClient(timeout=self.timeout_s, follow_redirects=False)

    @classmethod
    def from_env(cls, *, prefix: str = "CONTINUITY_ASR_", client: Any | None = None) -> "OpenAICompatibleSpeechRecognizer":
        endpoint = os.environ.get(prefix + "ENDPOINT", "").strip()
        model = os.environ.get(prefix + "MODEL", "").strip()
        if not endpoint or not model:
            raise VoiceProviderConfigurationError(f"Set {prefix}ENDPOINT and {prefix}MODEL")
        key_name = os.environ.get(prefix + "API_KEY_ENV", prefix + "API_KEY")
        api_key = os.environ.get(key_name)
        return cls(endpoint_url=endpoint, model=model, api_key=api_key,
                   timeout_s=float(os.environ.get(prefix + "TIMEOUT_S", "30")),
                   language=os.environ.get(prefix + "LANGUAGE") or None, client=client)

    async def transcribe(self, audio: Any) -> Mapping[str, Any]:
        if not isinstance(audio, (bytes, bytearray, memoryview)):
            raise TypeError("audio must be bytes-like")
        data = bytes(audio)
        if not data or len(data) > self.max_audio_bytes:
            raise ValueError("audio payload is empty or too large")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        form: dict[str, str] = {"model": self.model, "response_format": "json"}
        if self.language:
            form["language"] = self.language
        try:
            response = await self._client.post(
                self.endpoint_url, headers=headers, data=form,
                files={"file": (self.filename, data, self.content_type)}, timeout=self.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise VoiceProviderResponseError(f"asr_request_failed:{type(exc).__name__}") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("text"), str):
            raise VoiceProviderResponseError("asr_response_missing_text")
        return {"text": payload["text"][:12000], "source": "openai_compatible_asr",
                **({"language": payload["language"][:30]} if isinstance(payload.get("language"), str) else {})}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class AudioSink(Protocol):
    async def play(self, audio: bytes, *, audio_format: str) -> Mapping[str, Any]: ...
    async def stop(self) -> None: ...


class OpenAICompatibleTextToSpeech:
    """TTS endpoint adapter that sends returned audio to a host-supplied sink."""
    def __init__(self, *, endpoint_url: str, model: str, sink: AudioSink,
                 api_key: str | None = None, timeout_s: float = 30.0,
                 default_voice: str | None = None, response_format: str = "wav",
                 client: Any | None = None, max_response_bytes: int = 32_000_000):
        _validate_endpoint(endpoint_url)
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise VoiceProviderConfigurationError("model must be a non-empty string <= 200 chars")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise VoiceProviderConfigurationError("api_key must be omitted or non-empty")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(float(timeout_s)) or timeout_s <= 0:
            raise VoiceProviderConfigurationError("timeout_s must be finite and positive")
        if response_format not in {"wav", "mp3", "opus", "aac", "flac", "pcm"}:
            raise VoiceProviderConfigurationError("unsupported response_format")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 1024 <= max_response_bytes <= 128_000_000:
            raise VoiceProviderConfigurationError("max_response_bytes must be in [1024,128000000]")
        if not callable(getattr(sink, "play", None)) or not callable(getattr(sink, "stop", None)):
            raise VoiceProviderConfigurationError("sink must implement async play() and stop()")
        self.endpoint_url, self.model, self.sink, self.api_key = endpoint_url, model.strip(), sink, api_key
        self.timeout_s, self.default_voice = float(timeout_s), default_voice
        self.response_format, self.max_response_bytes = response_format, max_response_bytes
        self._owns_client = client is None
        self._client = client or _httpx().AsyncClient(timeout=self.timeout_s, follow_redirects=False)
        self._active: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, *, sink: AudioSink, prefix: str = "CONTINUITY_TTS_",
                 client: Any | None = None) -> "OpenAICompatibleTextToSpeech":
        endpoint = os.environ.get(prefix + "ENDPOINT", "").strip()
        model = os.environ.get(prefix + "MODEL", "").strip()
        if not endpoint or not model:
            raise VoiceProviderConfigurationError(f"Set {prefix}ENDPOINT and {prefix}MODEL")
        key_env = os.environ.get(prefix + "API_KEY_ENV", prefix + "API_KEY")
        api_key = os.environ.get(key_env)
        try:
            timeout = float(os.environ.get(prefix + "TIMEOUT_S", "30"))
        except ValueError as exc:
            raise VoiceProviderConfigurationError(f"{prefix}TIMEOUT_S must be numeric") from exc
        return cls(endpoint_url=endpoint, model=model, sink=sink, api_key=api_key, timeout_s=timeout,
                   default_voice=os.environ.get(prefix + "VOICE") or None,
                   response_format=os.environ.get(prefix + "FORMAT", "wav"), client=client)

    async def speak(self, text: str, *, voice: str | None = None) -> Mapping[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("text must contain 1..4000 characters")
        payload = {"model": self.model, "input": text, "response_format": self.response_format}
        selected_voice = voice or self.default_voice
        if selected_voice:
            payload["voice"] = selected_voice
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        started = time.monotonic()
        try:
            response = await self._client.post(self.endpoint_url, headers=headers, json=payload, timeout=self.timeout_s)
            response.raise_for_status()
            audio = response.content
            if not audio or len(audio) > self.max_response_bytes:
                raise VoiceProviderResponseError("tts_audio_empty_or_too_large")
            sink_result = await self.sink.play(audio, audio_format=self.response_format)
            if not isinstance(sink_result, Mapping):
                raise VoiceProviderResponseError("audio_sink_invalid_result")
            if sink_result.get("status") in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                return {**dict(sink_result), "provider": "openai_compatible_tts",
                        "latency_s": max(0.0, time.monotonic() - started)}
            return {**dict(sink_result), "status": sink_result.get("status", "SUCCEEDED"),
                    "provider": "openai_compatible_tts", "audio_bytes": len(audio),
                    "latency_s": max(0.0, time.monotonic() - started)}
        except asyncio.CancelledError:
            await self.stop()
            raise
        except VoiceProviderResponseError:
            raise
        except Exception as exc:
            raise VoiceProviderResponseError(f"tts_request_failed:{type(exc).__name__}") from exc
        finally:
            if task is not None:
                self._active.discard(task)

    async def stop(self) -> None:
        await self.sink.stop()

    async def aclose(self) -> None:
        await self.stop()
        if self._owns_client:
            await self._client.aclose()


class SubprocessAudioSink:
    """Pipe encoded audio to an explicitly configured player, without a shell.

    Example command: ``["ffplay", "-autoexit", "-nodisp", "-loglevel", "error", "-i", "pipe:0"]``.
    It is disabled until the host supplies a command and intentionally never chooses one
    based on model output.
    """
    def __init__(self, command: Sequence[str], *, timeout_s: float = 120.0):
        if not command or any(not isinstance(x, str) or not x or len(x) > 500 for x in command):
            raise ValueError("command must be a non-empty list of bounded arguments")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(float(timeout_s)) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        self.command = tuple(command)
        self.timeout_s = float(timeout_s)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def play(self, audio: bytes, *, audio_format: str) -> Mapping[str, Any]:
        if not isinstance(audio, bytes) or not audio:
            raise ValueError("audio must be non-empty bytes")
        async with self._lock:
            if self._process is not None and self._process.returncode is None:
                raise RuntimeError("audio_sink_busy")
            proc = await asyncio.create_subprocess_exec(*self.command, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            self._process = proc
        try:
            if proc.stdin is None:
                raise RuntimeError("audio_player_stdin_unavailable")
            proc.stdin.write(audio)
            await asyncio.wait_for(proc.stdin.drain(), timeout=self.timeout_s)
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=self.timeout_s)
            if proc.returncode != 0:
                return {"status": "FAILED", "error_code": "AUDIO_PLAYER_EXIT_NONZERO", "returncode": proc.returncode}
            return {"status": "SUCCEEDED", "audio_format": audio_format}
        except asyncio.TimeoutError:
            await self.stop()
            return {"status": "TIMED_OUT", "error_code": "AUDIO_PLAYER_TIMEOUT"}
        except asyncio.CancelledError:
            await self.stop()
            raise
        finally:
            async with self._lock:
                if self._process is proc:
                    self._process = None

    async def stop(self) -> None:
        async with self._lock:
            proc = self._process
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

__all__ = ["VoiceProviderConfigurationError", "VoiceProviderResponseError", "OpenAICompatibleSpeechRecognizer", "AudioSink", "OpenAICompatibleTextToSpeech", "SubprocessAudioSink"]
