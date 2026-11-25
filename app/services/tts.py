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
        profile_id: str | None = None,
        language: str = "Hindi",
        engine: str = "chatterbox",
    ) -> bytes:
        if not text or not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        payload = {
            "text": text.strip(),
            "language": self.language_code(language),
            "engine": engine,
            "max_chunk_chars": 800,
            "crossfade_ms": 50,
            "normalize": True,
        }
        if profile_id:
            payload["profile_id"] = profile_id

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


class KVoiceTTSProvider:
    """Adapter for the hosted kVoive /v1/tts API used by F5PdfStudio."""

    def __init__(self, api_url: str, api_key: str):
        self.base_url = (api_url or "").rstrip("/")
        self.api_key = api_key or ""
        if not self.base_url or not self.api_key:
            raise ValueError("Configure the kVoice API URL and API key in Project API Settings.")

    async def list_voices(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                f"{self.base_url}/v1/voices",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            data = response.json()
        if isinstance(data, list):
            return [item if isinstance(item, dict) else {"id": str(item), "name": str(item)} for item in data]
        if isinstance(data, dict):
            voices = data.get("voices", data.get("items", []))
            if isinstance(voices, list):
                return [item if isinstance(item, dict) else {"id": str(item), "name": str(item)} for item in voices]
        raise RuntimeError("kVoice /v1/voices returned an unexpected response.")

    async def synthesize_bytes(self, text: str, profile_id: str, language: str = "Hindi", engine: str = "") -> bytes:
        if not text or not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        lang = (language or "Hindi").strip().lower()
        lang = {"hindi": "hi", "english": "en"}.get(lang, lang)
        payload = {
            "text": text.strip(),
            "voice": profile_id,
            "language": lang,
            "format": "wav",
            "respect_lines": True,
            "master": True,
            "content_type": None,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800)) as client:
            response = await client.post(
                f"{self.base_url}/v1/tts",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            audio = response.content
        if not audio.startswith(b"RIFF") or b"WAVE" not in audio[:16]:
            raise RuntimeError("kVoice /v1/tts did not return a WAV file.")
        return audio


class GeminiTTSProvider(TTSProvider):
    """Gemini API text-to-speech adapter using the project's Gemini API key."""

    VOICES = [
        "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede",
        "Autonoe", "Callirrhoe", "Charon", "Despina", "Enceladus",
        "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia",
        "Leda", "Orus", "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia",
        "Sadaltager", "Schedar", "Sulafat", "Umbriel", "Vindemiatrix",
        "Zephyr", "Zubenelgenubi",
    ]

    def __init__(self):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured in .env")
        from google import genai
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options={"api_version": "v1beta"},
        )
        self.model = settings.gemini_tts_model or "gemini-3.8-flash-tts"

    @classmethod
    async def list_voices(cls) -> list[dict]:
        return [
            {"id": voice, "voice_id": voice, "name": voice, "language": "Hindi"}
            for voice in cls.VOICES
        ]

    async def synthesize_bytes(
        self, text: str, profile_id: str, language: str = "Hindi", engine: str = "",
    ) -> bytes:
        if not text or not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        if profile_id not in self.VOICES:
            raise ValueError(f"Unsupported Gemini TTS voice: {profile_id}")
        from google.genai import types
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=self.model,
            contents=[text.strip()],
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    language_code=self._language_code(language),
                    voice_config=types.VoiceConfig(
                        voice=profile_id,
                    ),
                ),
            ),
        )
        for candidate in (getattr(response, "candidates", None) or []):
            content = getattr(candidate, "content", None)
            for part in (getattr(content, "parts", None) or []):
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline else None
                if data:
                    audio = bytes(data)
                    if audio.startswith(b"RIFF") and b"WAVE" in audio[:16]:
                        return audio
        raise RuntimeError("Gemini TTS returned no WAV audio data.")

    @staticmethod
    def _language_code(language: str) -> str:
        value = (language or "Hindi").strip().lower()
        return {
            "hindi": "hi-IN", "english": "en-US", "tamil": "ta-IN",
            "telugu": "te-IN", "bengali": "bn-IN", "marathi": "mr-IN",
            "gujarati": "gu-IN", "kannada": "kn-IN", "malayalam": "ml-IN",
            "punjabi": "pa-IN", "urdu": "ur-IN",
        }.get(value, value if "-" in value else "hi-IN")

    async def synthesize(
        self, text: str, output_path: str, voice_id: str | None = None,
        reference_audio: str | None = None, reference_text: str | None = None,
    ) -> str:
        audio = await self.synthesize_bytes(text, voice_id or "Kore")
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return str(path)


class VoiceStudioTTSProvider:
    """OpenAI-compatible Voice Studio API adapter."""
    def __init__(self, api_url: str, api_key: str = ""):
        self.base_url = (api_url or "http://127.0.0.1:3900").rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _get_json(self, path: str):
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def list_voices(self) -> list[dict]:
        data = await self._get_json("/v1/audio/voices")
        if isinstance(data, dict): data = data.get("voices", data.get("items", []))
        if not isinstance(data, list): raise RuntimeError("Voice Studio voices response is invalid.")
        return [x if isinstance(x, dict) else {"id": str(x), "name": str(x)} for x in data]

    async def list_models(self) -> list[dict]:
        data = await self._get_json("/v1/models")
        if isinstance(data, dict): data = data.get("data", data.get("models", []))
        return data if isinstance(data, list) else []

    async def get_model(self, model_id: str) -> dict:
        from urllib.parse import quote
        data = await self._get_json(f"/v1/models/{quote(model_id, safe='')}")
        return data if isinstance(data, dict) else {"data": data}

    async def synthesize_bytes(self, text: str, profile_id: str | None = None, language: str = "Hindi", engine: str = "") -> bytes:
        if not text or not text.strip(): raise ValueError("Cannot synthesize empty text.")
        models = await self.list_models()
        model_id = (engine if engine and engine not in {"chatterbox", "chatterbox_turbo", "qwen", "qwen_custom_voice", "luxtts", "tada", "kokoro"} else None) or (str(models[0].get("id")) if models and isinstance(models[0], dict) else "tts-1")
        payload = {"model": model_id, "input": text.strip(), "voice": profile_id or "alloy", "response_format": "wav"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30)) as client:
            response = await client.post(f"{self.base_url}/v1/audio/speech", headers=self.headers, json=payload)
            response.raise_for_status()
            audio = response.content
        if not audio.startswith(b"RIFF") or b"WAVE" not in audio[:16]:
            raise RuntimeError("Voice Studio /v1/audio/speech did not return WAV audio; verify WAV response_format support.")
        return audio


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
