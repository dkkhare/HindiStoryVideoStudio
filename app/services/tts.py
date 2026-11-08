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
    """Adapter for Gemini's native text-to-speech model."""
    VOICES = (
        "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede",
        "Autonoe", "Callirrhoe", "Charon", "Despina", "Enceladus", "Erinome",
        "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia", "Leda", "Orus",
        "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager",
        "Schedar", "Sulafat", "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi",
    )

    async def list_voices(self) -> list[dict]:
        return [{"id": name, "name": name, "language": "multilingual"} for name in self.VOICES]

    async def synthesize_bytes(self, text: str, profile_id: str, language: str = "Hindi", engine: str = "") -> bytes:
        if not text or not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is missing; configure it in .env.")
        if profile_id not in self.VOICES:
            raise ValueError(f"Unsupported Gemini TTS voice: {profile_id}")

        def generate() -> bytes:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=settings.gemini_api_key)
            response = client.models.generate_content(
                model=settings.gemini_tts_model,
                contents=text.strip(),
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=profile_id)
                        )
                    ),
                ),
            )
            for candidate in (getattr(response, "candidates", None) or []):
                content = getattr(candidate, "content", None)
                for part in (getattr(content, "parts", None) or []):
                    inline = getattr(part, "inline_data", None)
                    audio = getattr(inline, "data", None) if inline else None
                    if not audio:
                        continue
                    if isinstance(audio, str):
                        import base64
                        audio = base64.b64decode(audio)
                    audio = bytes(audio)
                    if audio.startswith(b"RIFF") and b"WAVE" in audio[:16]:
                        return audio
                    # Gemini 2.5 TTS emits 24 kHz mono 16-bit PCM.
                    buffer = io.BytesIO()
                    with wave.open(buffer, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(24000)
                        wav.writeframes(audio)
                    return buffer.getvalue()
            raise RuntimeError("Gemini returned no audio data for this text.")
        return await asyncio.to_thread(generate)

    async def synthesize(self, text: str, output_path: str, voice_id: str | None = None,
                         reference_audio: str | None = None, reference_text: str | None = None) -> str:
        if not voice_id:
            raise ValueError("Select a Gemini voice before generating audio.")
        audio = await self.synthesize_bytes(text, voice_id)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return str(path)


class VoiceStudioTTSProvider(TTSProvider):
    """Adapter for a local OpenAI-compatible Voice Studio server."""
    def __init__(self):
        self.base_url = settings.voicestudio_base_url.rstrip("/")
        self.timeout = httpx.Timeout(settings.voicestudio_timeout_seconds)

    async def list_voices(self) -> list[dict]:
        names = [x.strip() for x in settings.voicestudio_voices.split(",") if x.strip()]
        return [{"id": x, "name": x} for x in names]

    async def synthesize_bytes(self, text: str, profile_id: str, language: str = "Hindi", engine: str = "") -> bytes:
        if not text or not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        if not profile_id:
            raise ValueError("Select a Voice Studio voice before generating audio.")
        if not settings.voicestudio_api_key:
            raise RuntimeError("VOICESTUDIO_API_KEY is missing; configure it in .env.")
        headers = {
            "Authorization": f"Bearer {settings.voicestudio_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.voicestudio_model,
            "input": text.strip(),
            "voice": profile_id,
            "response_format": "wav",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/audio/speech", headers=headers, json=payload,
            )
            if not response.is_success:
                raise RuntimeError(
                    f"Voice Studio API {response.status_code}: {response.text[:2000]}"
                )
            audio = response.content
            content_type = response.headers.get("content-type", "unknown")
        if not audio.startswith(b"RIFF") or b"WAVE" not in audio[:16]:
            raise RuntimeError(
                "Voice Studio did not return WAV audio. Content-Type: " + content_type +
                ". Confirm /v1/audio/speech supports response_format='wav'."
            )
        return audio

    async def synthesize(self, text: str, output_path: str, voice_id: str | None = None,
                         reference_audio: str | None = None, reference_text: str | None = None) -> str:
        if not voice_id:
            raise ValueError("Select a Voice Studio voice before generating audio.")
        audio = await self.synthesize_bytes(text, voice_id)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return str(path)


async def list_tts_voices(provider: str) -> list[dict]:
    name = (provider or "").strip().lower()
    if name == "voicebox":
        return await VoiceboxTTSProvider().list_profiles()
    if name == "gemini":
        return await GeminiTTSProvider().list_voices()
    if name in {"voicestudio", "voice_studio"}:
        return await VoiceStudioTTSProvider().list_voices()
    raise ValueError(f"Unknown TTS provider: {provider}")


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
    if provider in {"voicestudio", "voice_studio"}:
        return VoiceStudioTTSProvider()
    if provider == "f5tts":
        return F5TTSProvider()
    raise ValueError(f"Unknown TTS provider: {provider}")
