from abc import ABC, abstractmethod
import asyncio
import io
import time
import wave
from pathlib import Path

import httpx

from app.config import settings


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(
        self,
        text: str,
        output_path: str,
        voice_id: str | None = None,
        reference_audio: str | None = None,
        reference_text: str | None = None,
    ) -> str:
        raise NotImplementedError


class VoiceboxTTSProvider(TTSProvider):
    """Adapter for the local Voicebox REST API."""

    def __init__(self):
        self.base_url = settings.voicebox_base_url.rstrip("/")
        self.timeout = httpx.Timeout(settings.voicebox_timeout_seconds)

    async def list_profiles(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}/profiles")
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, list):
            raise RuntimeError("Voicebox /profiles returned an unexpected response.")
        return data

    @staticmethod
    def language_code(language: str) -> str:
        supported = {
            "zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it",
            "he", "ar", "da", "el", "fi", "hi", "ms", "nl", "no", "pl",
            "sv", "sw", "tr",
        }
        value = (language or "Hindi").strip().lower()
        names = {
            "chinese": "zh", "english": "en", "japanese": "ja",
            "korean": "ko", "german": "de", "french": "fr",
            "russian": "ru", "portuguese": "pt", "spanish": "es",
            "italian": "it", "hebrew": "he", "arabic": "ar",
            "danish": "da", "greek": "el", "finnish": "fi",
            "hindi": "hi", "malay": "ms", "dutch": "nl",
            "norwegian": "no", "polish": "pl", "swedish": "sv",
            "swahili": "sw", "turkish": "tr",
        }
        code = names.get(value, value)
        if code not in supported:
            raise ValueError(
                f"Voicebox does not list support for '{language}'. "
                "Choose a language supported by Voicebox."
            )
        return code

    async def synthesize_bytes(
        self,
        text: str,
        profile_id: str,
        language: str = "Hindi",
        engine: str = "chatterbox",
    ) -> bytes:
        if not text or not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        payload = {
            "profile_id": profile_id,
            "text": text.strip(),
            "language": self.language_code(language),
            "engine": engine,
            "max_chunk_chars": 800,
            "crossfade_ms": 50,
            "normalize": True,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/generate", json=payload)
            response.raise_for_status()
            generation = response.json()

            generation_id = generation.get("id")
            if not generation_id:
                raise RuntimeError(
                    f"Voicebox did not return a generation ID: {generation!r}"
                )

            status = str(generation.get("status") or "").lower()
            deadline = time.monotonic() + settings.voicebox_generation_timeout_seconds

            while status not in {"completed", "complete", "success", "failed", "error"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Voicebox generation {generation_id} did not finish in time."
                    )
                await asyncio.sleep(1)
                history_response = await client.get(
                    f"{self.base_url}/history/{generation_id}"
                )
                history_response.raise_for_status()
                history = history_response.json()
                status = str(history.get("status") or "").lower()
                if status in {"failed", "error"}:
                    raise RuntimeError(
                        history.get("error") or f"Voicebox generation {generation_id} failed."
                    )

            if status in {"failed", "error"}:
                raise RuntimeError(
                    generation.get("error") or f"Voicebox generation {generation_id} failed."
                )

            audio_response = await client.get(
                f"{self.base_url}/audio/{generation_id}"
            )
            audio_response.raise_for_status()
            audio_bytes = audio_response.content

        if not audio_bytes.startswith(b"RIFF") or b"WAVE" not in audio_bytes[:16]:
            raise RuntimeError(
                "Voicebox audio endpoint did not return a WAV file."
            )
        return audio_bytes

    async def synthesize(
        self,
        text: str,
        output_path: str,
        voice_id: str | None = None,
        reference_audio: str | None = None,
        reference_text: str | None = None,
    ) -> str:
        if not voice_id:
            raise ValueError("Voicebox requires a voice profile ID.")
        audio = await self.synthesize_bytes(
            text=text,
            profile_id=voice_id,
            language="Hindi",
            engine=settings.voicebox_engine,
        )
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return str(path)


class GeminiTTSProvider(TTSProvider):
    async def synthesize(
        self, text: str, output_path: str, voice_id: str | None = None,
        reference_audio: str | None = None, reference_text: str | None = None,
    ) -> str:
        raise NotImplementedError(
            "Gemini TTS audio adapter is not implemented yet."
        )


class F5TTSProvider(TTSProvider):
    async def synthesize(
        self, text: str, output_path: str, voice_id: str | None = None,
        reference_audio: str | None = None, reference_text: str | None = None,
    ) -> str:
        if not reference_audio or not reference_text:
            raise ValueError(
                "F5-TTS requires reference_audio and reference_text for voice cloning"
            )
        payload = {
            "text": text,
            "ref_audio": reference_audio,
            "ref_text": reference_text,
        }
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(
                settings.f5tts_base_url.rstrip("/") + settings.f5tts_api_path,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        raise NotImplementedError(
            f"F5-TTS server responded; map its response format to WAV: {data!r}"
        )


def get_tts_provider(name: str | None = None) -> TTSProvider:
    provider = (name or settings.default_tts_provider).lower()
    if provider == "voicebox":
        return VoiceboxTTSProvider()
    if provider == "gemini":
        return GeminiTTSProvider()
    if provider == "f5tts":
        return F5TTSProvider()
    raise ValueError(f"Unknown TTS provider: {provider}")
