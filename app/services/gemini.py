
import asyncio
import json
import logging
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)

# Text model is selected through app settings.


SCREENPLAY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "duration_seconds": {"type": "integer"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "character_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "visual_lock": {"type": "string"},
                    "voice_profile": {"type": "string"}
                },
                "required": [
                    "character_id",
                    "name",
                    "description",
                    "visual_lock",
                    "voice_profile"
                ]
            }
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "duration_seconds": {"type": "integer"},
                    "summary": {"type": "string"},
                    "characters": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "image_prompt": {"type": "string"},
                    "video_prompt": {"type": "string"},
                    "dialogue_and_narration": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "segment_id": {"type": "string"},
                                "speaker": {"type": "string"},
                                "kind": {"type": "string"},
                                "text": {"type": "string"},
                                "estimated_duration_seconds": {
                                    "type": "number"
                                }
                            },
                            "required": [
                                "segment_id",
                                "speaker",
                                "kind",
                                "text",
                                "estimated_duration_seconds"
                            ]
                        }
                    }
                },
                "required": [
                    "scene_id",
                    "duration_seconds",
                    "summary",
                    "characters",
                    "image_prompt",
                    "video_prompt",
                    "dialogue_and_narration"
                ]
            }
        }
    },
    "required": [
        "title",
        "logline",
        "duration_seconds",
        "characters",
        "scenes"
    ]
}


class GeminiService:

    def __init__(self):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is missing")

        self.client = genai.Client(
            api_key=settings.gemini_api_key
        )

        self.model = settings.gemini_text_model
        self.image_model = settings.gemini_image_model

    # ---------------------------------------------------------
    # SCREENPLAY GENERATION
    # Existing functionality - preserved
    # ---------------------------------------------------------

    async def generate_screenplay(
        self,
        story_idea: str,
        duration_minutes: int,
        language: str,
        visual_style: str,
        aspect_ratio: str
    ) -> dict:

        target_seconds = duration_minutes * 60

        prompt = f"""
You are an experienced screenwriter, story editor, and visual production planner.

Create a complete, production-ready screenplay from the user's story idea.
Respect the genre, tone, setting, intended audience, cultural context, and
storytelling style implied by the idea. Do not assume a particular country,
culture, audience age, genre, or animation/live-action format unless specified.

STORY IDEA:
{story_idea}

TARGET DURATION:
{duration_minutes} minutes ({target_seconds} seconds).

LANGUAGE:
{language}

VISUAL STYLE:
{visual_style}

ASPECT RATIO:
{aspect_ratio}

SCENE LENGTH GUIDANCE:
Aim for scene durations near {settings.default_scene_seconds} seconds, adjusting
naturally when the action, dialogue, pacing, or story structure requires it.
All scene durations combined must equal approximately {target_seconds} seconds.
Maximum permitted deviation: 5 percent.

PRODUCTION REQUIREMENTS:

1. Divide the story into clear, visually producible scenes.
2. Give every character a permanent unique ID: CHAR_001, CHAR_002, etc.
3. CHARACTER CONSISTENCY:
   Write a detailed visual_lock for every character. Specify stable identity
   features such as appearance, colors, clothing, proportions, facial features,
   and distinctive accessories. Do not confuse stable identity with temporary
   scene states such as wet clothing, injury, dirt, emotion, or pose.
4. ASSET AND STATE CONTINUITY:
   Keep the identity of recurring objects and locations consistent. A character,
   object, or location may undergo story-driven changes. Describe those changes
   in the relevant scene instead of incorrectly freezing its condition.
   Preserve the result of a change in all later scenes until the screenplay
   explicitly changes it again.
5. Each scene must contain:
   - Scene ID
   - Duration
   - Summary
   - Character IDs appearing in the scene
   - Detailed image generation prompt
   - Motion-focused video generation prompt
   - Ordered dialogue and narration segments
6. Write all dialogue and narration in {language}. Use the writing system
   appropriate to that language. Do not translate dialogue into another
   language unless the story requests it.
7. Use NARRATOR for narration. Use the corresponding character ID for
   character dialogue.
8. Keep dialogue and narration short enough for the scene duration.
9. Estimate the duration of every spoken segment.
10. Maintain continuity of location, props, character identity, scene state,
    lighting, weather, and time of day.
11. Image prompts must describe the complete visual composition, camera
    framing, lighting, and character positions.
12. Video prompts must describe natural, physically plausible movements
    appropriate to the chosen visual style and story world.
13. Do not include subtitles, logos, written text, watermarks, or captions
    inside image prompts unless the story explicitly requires visible text.
14. Make the screenplay emotionally engaging and appropriate to the story's
    intended audience; do not impose a family or children's audience by default.
15. Return ONLY valid structured JSON matching the supplied schema.
""""""

        logger.info(
            "Generating screenplay using model: %s",
            self.model
        )

        try:
            interaction = await asyncio.to_thread(
                self.client.interactions.create,
                model=self.model,
                input=prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": SCREENPLAY_SCHEMA
                }
            )

            response_text = interaction.output_text

            if not response_text:
                raise ValueError(
                    "Gemini returned an empty screenplay response"
                )

            data = json.loads(response_text)

            if not isinstance(data, dict):
                raise ValueError(
                    "Gemini screenplay response is not a JSON object"
                )

            self._validate_structure(data)
            self._validate_duration(data, target_seconds)

            logger.info(
                "Screenplay generated successfully: %s scenes",
                len(data["scenes"])
            )

            return data

        except Exception:
            logger.exception(
                "Gemini screenplay generation failed"
            )
            raise

    # ---------------------------------------------------------
    # IMAGE GENERATION
    # New functionality
    # ---------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        output_path: Path,
        reference_images: list[Path] | None = None,
        aspect_ratio: str = "16:9",
        image_size: str = "1K"
    ) -> dict:
        """
        Generate one image using Gemini.

        Args:
            prompt:
                Detailed image-generation prompt.

            output_path:
                Destination PNG file.

            reference_images:
                Existing character, prop or location images.
                These are supplied to Gemini as visual references.

            aspect_ratio:
                Supported ratios include 16:9, 9:16, 1:1,
                4:3, 3:4, 3:2 and 2:3.

            image_size:
                1K, 2K or 4K.

        Returns:
            Dictionary containing saved file information.
        """

        if not prompt or not prompt.strip():
            raise ValueError("Image prompt cannot be empty")

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        reference_images = reference_images or []

        valid_references = []

        for reference in reference_images:
            reference = Path(reference)

            if not reference.exists():
                logger.warning(
                    "Reference image does not exist: %s",
                    reference
                )
                continue

            if reference.suffix.lower() not in (
                ".png", ".jpg", ".jpeg", ".webp"
            ):
                raise ValueError(
                    f"Unsupported reference image format: {reference}"
                )

            valid_references.append(reference)

        # Gemini 3.1 Flash Image supports up to 4 character
        # references and 10 object references in the documented
        # reference-image workflow. Keep the initial implementation
        # within that overall limit.
        if len(valid_references) > 14:
            raise ValueError(
                "A maximum of 14 reference images can be supplied "
                "in one generation request."
            )

        contents = [prompt]

        # Load copies so file handles are closed before the API call.
        for reference in valid_references:
            with Image.open(reference) as image:
                contents.append(image.copy())

        logger.info(
            "Generating image: %s | model=%s | references=%s",
            output_path.name,
            self.image_model,
            len(valid_references)
        )

        config = types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            ),
        )

        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.image_model,
                contents=contents,
                config=config
            )

            image_saved = False
            response_text = []

            for part in self._get_response_parts(response):

                if getattr(part, "text", None):
                    response_text.append(part.text)

                if getattr(part, "inline_data", None):
                    generated_image = part.as_image()

                    if generated_image is None:
                        continue

                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    generated_image.save(str(output_path))

                    image_saved = True
                    break

            if not image_saved:
                raise RuntimeError(
                    "Gemini returned no image data. "
                    + " ".join(response_text)
                )

            logger.info(
                "Image saved successfully: %s",
                output_path
            )

            return {
                "success": True,
                "path": str(output_path),
                "filename": output_path.name,
                "reference_count": len(valid_references),
                "model": self.image_model,
                "message": "Image generated successfully"
            }

        except Exception:
            logger.exception(
                "Gemini image generation failed: %s",
                output_path
            )
            raise

    @staticmethod
    def _get_response_parts(response):
        """
        Return response parts while tolerating SDK response
        objects that expose parts through candidates.
        """

        parts = getattr(response, "parts", None)

        if parts:
            return parts

        candidates = getattr(response, "candidates", None) or []

        result = []

        for candidate in candidates:
            content = getattr(candidate, "content", None)

            if content:
                result.extend(
                    getattr(content, "parts", None) or []
                )

        return result

    # ---------------------------------------------------------
    # SCREENPLAY VALIDATION
    # Existing functionality - preserved
    # ---------------------------------------------------------

    @staticmethod
    def _validate_structure(data: dict) -> None:

        required_fields = [
            "title",
            "logline",
            "duration_seconds",
            "characters",
            "scenes"
        ]

        for field in required_fields:
            if field not in data:
                raise ValueError(
                    f"Screenplay is missing required field: {field}"
                )

        if not isinstance(data["characters"], list):
            raise ValueError(
                "Screenplay characters must be a list"
            )

        if not isinstance(data["scenes"], list):
            raise ValueError(
                "Screenplay scenes must be a list"
            )

        for index, scene in enumerate(data["scenes"], start=1):

            required_scene_fields = [
                "scene_id",
                "duration_seconds",
                "summary",
                "characters",
                "image_prompt",
                "video_prompt",
                "dialogue_and_narration"
            ]

            for field in required_scene_fields:
                if field not in scene:
                    raise ValueError(
                        f"Scene {index} is missing field: {field}"
                    )

    @staticmethod
    def _validate_duration(data: dict, target: int) -> None:

        scenes = data.get("scenes", [])

        if not scenes:
            raise ValueError(
                "Gemini returned no scenes"
            )

        actual = sum(
            int(scene.get("duration_seconds", 0))
            for scene in scenes
        )

        if actual <= 0:
            raise ValueError(
                "Screenplay scene durations are invalid"
            )

        difference = abs(actual - target) / target

        if difference > 0.10:
            raise ValueError(
                f"Scene durations total {actual}s; "
                f"target is {target}s. "
                "Regenerate or edit screenplay."
            )
