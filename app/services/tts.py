from abc import ABC, abstractmethod
import httpx
from app.config import settings

class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, output_path: str, voice_id: str | None = None,
                         reference_audio: str | None = None, reference_text: str | None = None) -> str:
        raise NotImplementedError

class GeminiTTSProvider(TTSProvider):
    async def synthesize(self, text: str, output_path: str, voice_id: str | None = None,
                         reference_audio: str | None = None, reference_text: str | None = None) -> str:
        # Gemini TTS produces configured voices; it is not reference-voice cloning.
        raise NotImplementedError("Gemini TTS audio adapter is the next integration task; configure a supported Gemini TTS model and PCM-to-WAV writer.")

class F5TTSProvider(TTSProvider):
    async def synthesize(self, text: str, output_path: str, voice_id: str | None = None,
                         reference_audio: str | None = None, reference_text: str | None = None) -> str:
        if not reference_audio or not reference_text:
            raise ValueError("F5-TTS requires reference_audio and reference_text for voice cloning")
        payload = {
            "text": text,
            "ref_audio": reference_audio,
            "ref_text": reference_text,
        }
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(settings.f5tts_base_url.rstrip("/") + settings.f5tts_api_path, json=payload)
            response.raise_for_status()
            # Different Gradio/F5-TTS deployments return different response formats.
            # Save a returned local path or decode returned audio in the deployment adapter.
            data = response.json()
        raise NotImplementedError(f"F5-TTS server responded; map its response format to WAV in this adapter: {data!r}")

def get_tts_provider(name: str | None = None) -> TTSProvider:
    provider = (name or settings.default_tts_provider).lower()
    if provider == "gemini":
        return GeminiTTSProvider()
    if provider == "f5tts":
        return F5TTSProvider()
    raise ValueError(f"Unknown TTS provider: {provider}")
