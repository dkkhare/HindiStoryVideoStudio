
import asyncio
import json
import logging
import re
from pathlib import Path

from google import genai

from app.config import settings
from app.services.gemini import GeminiService

logger = logging.getLogger(__name__)

ASSET_BIBLE_SCHEMA = {
    "type": "object",
    "properties": {
        "props": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "prop_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "visual_lock": {"type": "string"}
                },
                "required": [
                    "prop_id", "name", "description", "visual_lock"
                ]
            }
        },
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "visual_lock": {"type": "string"}
                },
                "required": [
                    "location_id", "name", "description", "visual_lock"
                ]
            }
        },
        "scene_assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "prop_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "location_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": [
                    "scene_id", "prop_ids", "location_ids"
                ]
            }
        },
        "asset_states": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "entity_id": {"type": "string"},
                                "entity_type": {"type": "string"},
                                "state": {"type": "string"},
                                "transition_from_previous": {"type": "string"}
                            },
                            "required": ["entity_id", "entity_type", "state", "transition_from_previous"]
                        }
                    }
                },
                "required": ["scene_id", "entities"]
            }
        }
    },
    "required": ["props", "locations", "scene_assets", "asset_states"]
}


class ImageGenerationService:

    def __init__(self):
        self.gemini = GeminiService()

        self.client = genai.Client(
            api_key=settings.gemini_api_key
        )

        self.text_model = settings.gemini_text_model
        self.semaphore = asyncio.Semaphore(
            max(1, settings.max_parallel_image_jobs)
        )

    # ---------------------------------------------------------
    # FILE HELPERS
    # ---------------------------------------------------------

    @staticmethod
    def safe_filename(value: str) -> str:
        value = re.sub(r"[^\w.-]+", "_", str(value), flags=re.UNICODE)
        return value.strip("._")[:100] or "asset"

    @staticmethod
    def write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary = path.with_suffix(path.suffix + ".tmp")

        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        temporary.replace(path)

    @staticmethod
    def read_json(path: Path, default=None):
        if not path.exists():
            return default

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Could not read JSON file: %s", path)
            return default

    @staticmethod
    def valid_image(path: Path) -> bool:
        if not path.exists() or path.stat().st_size < 1000:
            return False

        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()

            return True

        except Exception:
            return False

    @staticmethod
    def relative_path(project_path: Path, file_path: Path) -> str:
        return file_path.relative_to(project_path).as_posix()

    # ---------------------------------------------------------
    # MANIFEST
    # ---------------------------------------------------------

    def get_manifest(self, project_path: Path) -> dict:
        manifest_path = project_path / "image_manifest.json"

        manifest = self.read_json(manifest_path, None)

        if not isinstance(manifest, dict):
            manifest = {
                "version": 1,
                "status": "pending",
                "references": {
                    "status": "pending",
                    "characters": {},
                    "props": {},
                    "locations": {}
                },
                "scenes": {},
                "statistics": {
                    "references_total": 0,
                    "references_completed": 0,
                    "scenes_total": 0,
                    "scenes_completed": 0,
                    "failed": 0
                }
            }

        return manifest

    def save_manifest(self, project_path: Path, manifest: dict) -> None:
        self.write_json(
            project_path / "image_manifest.json",
            manifest
        )

    def update_statistics(self, manifest: dict) -> None:
        references = manifest.get("references", {})

        reference_groups = [
            references.get("characters", {}),
            references.get("props", {}),
            references.get("locations", {})
        ]

        all_references = [
            item
            for group in reference_groups
            for item in group.values()
        ]

        scenes = list(manifest.get("scenes", {}).values())

        manifest["statistics"] = {
            "references_total": len(all_references),
            "references_completed": sum(
                1 for item in all_references
                if item.get("status") == "completed"
            ),
            "scenes_total": len(scenes),
            "scenes_completed": sum(
                1 for item in scenes
                if item.get("status") == "completed"
            ),
            "failed": sum(
                1 for item in all_references + scenes
                if item.get("status") == "failed"
            )
        }

    # ---------------------------------------------------------
    # ASSET BIBLE
    # Extract recurring props and locations from screenplay.
    # ---------------------------------------------------------

    async def generate_asset_bible(
        self,
        project_path: Path,
        screenplay: dict,
        force_refresh: bool = False
    ) -> dict:

        output_path = project_path / "asset_bible.json"

        if output_path.exists() and not force_refresh:
            existing = self.read_json(output_path)

            if (
                isinstance(existing, dict)
                and isinstance(existing.get("asset_states"), list)
                and len(existing["asset_states"]) == len(screenplay.get("scenes", []))
            ):
                try:
                    self.validate_asset_bible(existing, screenplay)
                    logger.info("Using existing asset bible with valid state continuity data")
                    return existing
                except (TypeError, ValueError, KeyError):
                    logger.warning("Existing asset bible has incomplete continuity data; regenerating")

        screenplay_summary = {
            "title": screenplay.get("title"),
            "logline": screenplay.get("logline"),
            "characters": screenplay.get("characters", []),
            "scenes": [
                {
                    "scene_id": scene.get("scene_id"),
                    "summary": scene.get("summary"),
                    "image_prompt": scene.get("image_prompt"),
                    "video_prompt": scene.get("video_prompt"),
                    "characters": scene.get("characters", [])
                }
                for scene in screenplay.get("scenes", [])
            ]
        }

        prompt = f"""
You are a professional production designer and visual continuity supervisor.
Follow the project's selected visual style and the screenplay's own genre,
setting, culture, tone, and medium.

Analyze this complete screenplay and create an ASSET BIBLE.

SCREENPLAY:
{json.dumps(screenplay_summary, ensure_ascii=False, indent=2)}

TASK 1: PROPS

Identify all important recurring physical objects.

Include recurring or story-critical physical objects identified
from this screenplay. Do not create a fixed category list or
invent objects that are not supported by the story.

Give every prop a permanent ID:
PROP_001, PROP_002, etc.

For each prop provide:
- Exact name
- Description
- Detailed visual_lock

The visual_lock must describe permanent identity only:
shape, proportions, color, material, surface texture,
and distinctive identifying features. Do not place temporary
story conditions in visual_lock; describe those in asset_states.

Do not create duplicate entries for the same object.

TASK 2: LOCATIONS

Identify all important recurring locations.

Give each location a permanent ID:
LOC_001, LOC_002, etc.

For each location provide:
- Exact name
- Description
- Detailed visual_lock

The visual_lock must describe stable environmental identity:
architecture, layout, terrain, permanent background elements,
and characteristic vegetation. Describe story-driven changes
such as weather, damage, lighting, or temporary activity in asset_states.

Do not create duplicate entries for the same location.

TASK 3: SCENE ASSET MAPPING

For every screenplay scene, identify:
- The prop IDs actually appearing in that scene.
- The location IDs where that scene takes place.

Only use IDs defined in the asset bible.

IMPORTANT:

1. Do not invent unnecessary props or locations.
2. Maintain exact continuity across scenes.
3. Do not create a new ID for an existing asset.
4. Include every scene, even if its asset lists are empty.
5. Character IDs already exist in the screenplay.
   Do not create new characters.
6. Return only JSON matching the supplied schema.

TASK 4: SCENE-BY-SCENE ASSET STATE CONTINUITY

Track story-driven state changes for any recurring visual entity, including
characters, props, and locations. Do not assume a fixed list of properties.
Infer relevant state details from the screenplay itself.

For every scene, return one asset_states entry with its scene_id and an
entities array. Include every character, prop, and location that appears in the scene
according to the screenplay and scene asset mapping. Each entity record
must contain a complete visible-state snapshot, even when no temporary
change has occurred. Each entity record must contain:
- entity_id: an existing character_id, prop_id, or location_id
- entity_type: character, prop, or location
- state: a concise but sufficiently detailed description of the entity's
  complete visible state in this scene. If no temporary change is specified,
  explicitly state that its permanent identity is unchanged.
- transition_from_previous: explain what changed from its prior scene state.
  For the first appearance, describe its initial state. If nothing changed,
  explicitly say that the previous state is unchanged.

Never invent a state change that the screenplay does not establish.
A state change persists into later scenes until the screenplay changes it.
Do not confuse permanent identity details in visual_lock with temporary
story states. Do not use hard-coded object-specific property lists.
Use the same entity ID consistently across all scenes.
Return an asset_states entry for every scene, even when entities is empty.
"""

        logger.info("Generating project asset bible")

        interaction = await asyncio.to_thread(
            self.client.interactions.create,
            model=self.text_model,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": ASSET_BIBLE_SCHEMA
            }
        )

        response_text = interaction.output_text

        if not response_text:
            raise RuntimeError(
                "Gemini returned an empty asset bible"
            )

        asset_bible = json.loads(response_text)

        self.validate_asset_bible(asset_bible, screenplay)

        self.write_json(output_path, asset_bible)

        logger.info(
            "Asset bible created: %s props, %s locations",
            len(asset_bible["props"]),
            len(asset_bible["locations"])
        )

        return asset_bible

    @staticmethod
    def validate_asset_bible(
        data: dict,
        screenplay: dict
    ) -> None:

        for field in ("props", "locations", "scene_assets"):
            if not isinstance(data.get(field), list):
                raise ValueError(
                    f"Asset bible field '{field}' must be a list"
                )

        scene_ids = {
            scene.get("scene_id")
            for scene in screenplay.get("scenes", [])
        }

        mapped_scene_ids = {
            item.get("scene_id")
            for item in data["scene_assets"]
        }

        missing = scene_ids - mapped_scene_ids

        if missing:
            raise ValueError(
                "Asset bible is missing scene mappings: "
                + ", ".join(sorted(missing))
            )

        prop_ids = set()
        location_ids = set()

        for prop in data["props"]:
            if not all(
                prop.get(field)
                for field in (
                    "prop_id", "name", "description", "visual_lock"
                )
            ):
                raise ValueError("Invalid prop in asset bible")

            if prop["prop_id"] in prop_ids:
                raise ValueError("Duplicate prop ID")

            prop_ids.add(prop["prop_id"])

        for location in data["locations"]:
            if not all(
                location.get(field)
                for field in (
                    "location_id", "name", "description", "visual_lock"
                )
            ):
                raise ValueError("Invalid location in asset bible")

            if location["location_id"] in location_ids:
                raise ValueError("Duplicate location ID")

            location_ids.add(location["location_id"])

        for mapping in data["scene_assets"]:
            if not set(mapping.get("prop_ids", [])).issubset(prop_ids):
                raise ValueError(
                    f"Unknown prop ID in scene {mapping['scene_id']}"
                )

            if not set(
                mapping.get("location_ids", [])
            ).issubset(location_ids):
                raise ValueError(
                    f"Unknown location ID in scene {mapping['scene_id']}"
                )

        valid_entities = {
            item.get("character_id")
            for item in screenplay.get("characters", [])
        } | prop_ids | location_ids

        state_records = data.get("asset_states", [])
        if not isinstance(state_records, list):
            raise ValueError("Asset bible asset_states must be a list")

        state_scene_ids = set()
        for state_record in state_records:
            scene_id = state_record.get("scene_id")
            if scene_id not in scene_ids:
                raise ValueError(f"Unknown scene ID in asset states: {scene_id}")
            if scene_id in state_scene_ids:
                raise ValueError(f"Duplicate asset state record for scene: {scene_id}")
            state_scene_ids.add(scene_id)

            entities = state_record.get("entities", [])
            if not isinstance(entities, list):
                raise ValueError(f"Asset states for scene {scene_id} must contain an entities list")

            for entity in entities:
                entity_id = entity.get("entity_id")
                if entity_id not in valid_entities:
                    raise ValueError(f"Unknown entity ID in scene state: {entity_id}")
                if not entity.get("state"):
                    raise ValueError(f"Missing state description for entity {entity_id}")

        scene_lookup = {
            scene.get("scene_id"): scene
            for scene in screenplay.get("scenes", [])
        }
        mapping_lookup = {
            item.get("scene_id"): item
            for item in data["scene_assets"]
        }
        for state_record in state_records:
            scene_id = state_record.get("scene_id")
            scene = scene_lookup.get(scene_id, {})
            mapping = mapping_lookup.get(scene_id, {})
            expected_entities = (
                set(scene.get("characters", []))
                | set(mapping.get("prop_ids", []))
                | set(mapping.get("location_ids", []))
            )
            actual_entities = {
                entity.get("entity_id")
                for entity in state_record.get("entities", [])
            }
            missing_entities = expected_entities - actual_entities
            if missing_entities:
                raise ValueError(
                    f"Scene {scene_id} is missing state snapshots for: "
                    + ", ".join(sorted(missing_entities))
                )

        missing_state_scenes = scene_ids - state_scene_ids
        if missing_state_scenes:
            raise ValueError(
                "Asset states are missing scenes: "
                + ", ".join(sorted(missing_state_scenes))
            )

    # ---------------------------------------------------------
    # IMAGE PROMPTS
    # ---------------------------------------------------------

    @staticmethod
    def character_reference_prompt(
        character: dict,
        visual_style: str
    ) -> str:

        return f"""
Create a professional CHARACTER MASTER REFERENCE SHEET.

CHARACTER:
{character.get("name", "")}

DESCRIPTION:
{character.get("description", "")}

PERMANENT VISUAL IDENTITY:
{character.get("visual_lock", "")}

ART STYLE:
{visual_style}

IMAGE REQUIREMENTS:

Show exactly one character.

Full-body view, entire body visible from head to feet.
Three-quarter front view, standing naturally.
Neutral relaxed pose.
Arms and legs clearly visible.
Face clearly visible and recognizable.
Plain simple neutral studio background.
Soft, even professional character-design lighting.

The character must match the permanent identity,
proportions, facial features, and distinctive features
specified above. This is a neutral master identity reference.
Scene-specific state instructions may change temporary
appearance details when the screenplay requires it.

Do not add other characters.
Do not add props.
Do not invent temporary conditions or wardrobe changes.
Do not create a collage.
Do not include labels, letters, text or watermark.
"""

    @staticmethod
    def prop_reference_prompt(
        prop: dict,
        visual_style: str
    ) -> str:

        return f"""
Create a professional PROP MASTER REFERENCE IMAGE.

PROP:
{prop.get("name", "")}

DESCRIPTION:
{prop.get("description", "")}

PERMANENT VISUAL IDENTITY:
{prop.get("visual_lock", "")}

ART STYLE:
{visual_style}

Show exactly one clearly visible object.

Use a three-quarter view.
Show its complete shape and proportions.
Use a simple neutral studio background.
Use soft professional lighting.

Preserve the exact material, shape, proportions, color,
surface and texture specified above.

This is the permanent prop reference for the film.

IMPORTANT:
Show the object's stable identity clearly. Do not invent or
emphasize a temporary story condition in this master reference;
the scene-specific state instructions will define its condition
in each scene. Do not add extra objects.
No labels, text, letters or watermark.
"""

    @staticmethod
    def location_reference_prompt(
        location: dict,
        visual_style: str
    ) -> str:

        return f"""
Create a permanent LOCATION MASTER REFERENCE IMAGE.

LOCATION:
{location.get("name", "")}

DESCRIPTION:
{location.get("description", "")}

PERMANENT VISUAL IDENTITY:
{location.get("visual_lock", "")}

ART STYLE:
{visual_style}

Show the complete environment as a wide establishing shot.

Clearly establish:
- Layout and spatial arrangement
- Architecture
- Ground and terrain
- Vegetation
- Background elements
- Lighting
- Color palette
- Environmental atmosphere

Do not include characters.
Do not add objects that are not part of the location.
No text, labels, logos or watermark.

This location must remain visually consistent
whenever it appears in the film.
"""

    @staticmethod
    def scene_image_prompt(
        scene: dict,
        screenplay: dict,
        asset_bible: dict,
        aspect_ratio: str,
        visual_style: str = ""
    ) -> str:

        character_map = {
            item["character_id"]: item
            for item in screenplay.get("characters", [])
        }

        mapping = next(
            (
                item for item in asset_bible.get("scene_assets", [])
                if item.get("scene_id") == scene.get("scene_id")
            ),
            {}
        )

        prop_map = {
            item["prop_id"]: item
            for item in asset_bible.get("props", [])
        }

        location_map = {
            item["location_id"]: item
            for item in asset_bible.get("locations", [])
        }

        characters = [
            character_map[cid]
            for cid in scene.get("characters", [])
            if cid in character_map
        ]

        props = [
            prop_map[pid]
            for pid in mapping.get("prop_ids", [])
            if pid in prop_map
        ]

        locations = [
            location_map[lid]
            for lid in mapping.get("location_ids", [])
            if lid in location_map
        ]

        state_record = next(
            (
                item for item in asset_bible.get("asset_states", [])
                if item.get("scene_id") == scene.get("scene_id")
            ),
            {"scene_id": scene.get("scene_id"), "entities": []}
        )

        return f"""
Create one cinematic production-ready STORY FRAME.

FILM:
{screenplay.get("title", "")}

SCENE:
{scene.get("scene_id", "")}

SCENE SUMMARY:
{scene.get("summary", "")}

ORIGINAL SCENE IMAGE PROMPT:
{scene.get("image_prompt", "")}

ASPECT RATIO:
{aspect_ratio}

PROJECT VISUAL STYLE:
{visual_style or "Follow the visual style established by the screenplay and approved reference images."}

CHARACTER IDENTITY LOCKS:
{json.dumps(characters, ensure_ascii=False, indent=2)}

PROP IDENTITY LOCKS:
{json.dumps(props, ensure_ascii=False, indent=2)}

LOCATION IDENTITY LOCKS:
{json.dumps(locations, ensure_ascii=False, indent=2)}

CURRENT SCENE ASSET STATES:
{json.dumps(state_record.get("entities", []), ensure_ascii=False, indent=2)}

VISUAL CONTINUITY RULES:

1. The supplied reference images are authoritative.
2. Every character must match the permanent identity in its reference image.
3. Every prop must match the permanent identity in its reference image.
4. Every location must match the stable environment identity in its reference image.
5. Never redesign or reinterpret a reference asset.
6. Preserve permanent identity, while applying only the story-driven
   state changes specified for this scene.
7. Do not reset an entity to its initial condition after a later state change.
8. Do not add characters that are not in this scene.
9. Do not add unnecessary props.
10. Respect the original scene composition and action.
11. Keep lighting and environmental continuity.

CINEMATIC REQUIREMENTS:

Create one polished production-ready frame in the selected project visual style.

Use natural anatomy and physically plausible poses.
Use expressive but natural facial expressions.
Use coherent perspective and spatial relationships.
Use cinematic lighting and detailed environments.

Create one complete image, not a storyboard,
not a collage and not multiple panels.

Do not include subtitles, captions, written text,
logos, labels or watermarks.
"""

    # ---------------------------------------------------------
    # GENERATE ONE IMAGE WITH CONCURRENCY CONTROL
    # ---------------------------------------------------------

    async def create_image(
        self,
        prompt: str,
        output_path: Path,
        reference_images: list[Path],
        aspect_ratio: str,
        image_size: str,
        visual_style: str = ""
    ) -> dict:
        prompt = f"""
PROJECT VISUAL STYLE:
{visual_style or "Follow the visual style explicitly described in the scene prompt and approved reference images."}

STYLE AND CONTINUITY:
Apply the selected visual style consistently to this image. Do not impose a
different medium, genre, culture, location, or artistic treatment. Match the
approved reference images while respecting the current scene's requested state.
Do not add text, labels, captions, or watermarks unless explicitly required.

SCENE DESCRIPTION:
{prompt}
"""
        async with self.semaphore:
            return await self.gemini.generate_image(
                prompt=prompt,
                output_path=output_path,
                reference_images=reference_images,
                aspect_ratio=aspect_ratio,
                image_size=image_size
            )

    # ---------------------------------------------------------
    # REFERENCE IMAGE GENERATION
    # ---------------------------------------------------------

    async def generate_references(
        self,
        project_path: Path,
        screenplay: dict,
        visual_style: str,
        aspect_ratio: str = "16:9",
        image_size: str = "1K",
        force_regenerate: bool = False
    ) -> dict:

        project_path = Path(project_path)

        for folder in ("characters", "props", "locations", "references"):
            (project_path / folder).mkdir(
                parents=True,
                exist_ok=True
            )

        manifest = self.get_manifest(project_path)

        references = manifest.setdefault(
            "references",
            {
                "status": "pending",
                "characters": {},
                "props": {},
                "locations": {}
            }
        )

        for group in ("characters", "props", "locations"):
            references.setdefault(group, {})

        references["status"] = "running"
        self.save_manifest(project_path, manifest)

        try:
            asset_bible = await self.generate_asset_bible(
                project_path,
                screenplay
            )

            # -----------------------------
            # Character references
            # -----------------------------

            for character in screenplay.get("characters", []):

                asset_id = character["character_id"]

                filename = (
                    self.safe_filename(asset_id) + ".png"
                )

                output_path = (
                    project_path / "characters" / filename
                )

                record = references["characters"].setdefault(
                    asset_id,
                    {
                        "name": character.get("name"),
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "status": "pending"
                    }
                )

                if (
                    not force_regenerate
                    and self.valid_image(output_path)
                ):
                    record["status"] = "completed"
                    record["path"] = self.relative_path(
                        project_path, output_path
                    )
                    self.save_manifest(project_path, manifest)
                    continue

                try:
                    prompt = self.character_reference_prompt(
                        character,
                        visual_style
                    )

                    result = await self.create_image(
                        prompt,
                        output_path,
                        [],
                        "3:4",
                        image_size,
                        visual_style
                    )

                    record.update({
                        "status": "completed",
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "model": result["model"]
                    })

                except Exception as exc:
                    record.update({
                        "status": "failed",
                        "error": str(exc)
                    })
                    self.save_manifest(project_path, manifest)
                    raise

                self.save_manifest(project_path, manifest)

            # -----------------------------
            # Prop references
            # -----------------------------

            for prop in asset_bible.get("props", []):

                asset_id = prop["prop_id"]

                output_path = (
                    project_path / "props" /
                    (self.safe_filename(asset_id) + ".png")
                )

                record = references["props"].setdefault(
                    asset_id,
                    {
                        "name": prop.get("name"),
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "status": "pending"
                    }
                )

                if (
                    not force_regenerate
                    and self.valid_image(output_path)
                ):
                    record["status"] = "completed"
                    record["path"] = self.relative_path(
                        project_path, output_path
                    )
                    self.save_manifest(project_path, manifest)
                    continue

                try:
                    result = await self.create_image(
                        self.prop_reference_prompt(
                            prop, visual_style
                        ),
                        output_path,
                        [],
                        "1:1",
                        image_size,
                        visual_style
                    )

                    record.update({
                        "status": "completed",
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "model": result["model"]
                    })

                except Exception as exc:
                    record.update({
                        "status": "failed",
                        "error": str(exc)
                    })
                    self.save_manifest(project_path, manifest)
                    raise

                self.save_manifest(project_path, manifest)

            # -----------------------------
            # Location references
            # -----------------------------

            for location in asset_bible.get("locations", []):

                asset_id = location["location_id"]

                output_path = (
                    project_path / "locations" /
                    (self.safe_filename(asset_id) + ".png")
                )

                record = references["locations"].setdefault(
                    asset_id,
                    {
                        "name": location.get("name"),
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "status": "pending"
                    }
                )

                if (
                    not force_regenerate
                    and self.valid_image(output_path)
                ):
                    record["status"] = "completed"
                    record["path"] = self.relative_path(
                        project_path, output_path
                    )
                    self.save_manifest(project_path, manifest)
                    continue

                try:
                    result = await self.create_image(
                        self.location_reference_prompt(
                            location, visual_style
                        ),
                        output_path,
                        [],
                        aspect_ratio,
                        image_size,
                        visual_style
                    )

                    record.update({
                        "status": "completed",
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "model": result["model"]
                    })

                except Exception as exc:
                    record.update({
                        "status": "failed",
                        "error": str(exc)
                    })
                    self.save_manifest(project_path, manifest)
                    raise

                self.save_manifest(project_path, manifest)

            references["status"] = "completed"

            self.update_statistics(manifest)
            self.save_manifest(project_path, manifest)

            return {
                "success": True,
                "asset_bible": str(
                    project_path / "asset_bible.json"
                ),
                "manifest": str(
                    project_path / "image_manifest.json"
                ),
                "statistics": manifest["statistics"]
            }

        except Exception:
            references["status"] = "failed"
            self.update_statistics(manifest)
            self.save_manifest(project_path, manifest)
            logger.exception("Reference generation failed")
            raise

    # ---------------------------------------------------------
    # SCENE IMAGE GENERATION
    # ---------------------------------------------------------

    async def generate_scene_images(
        self,
        project_path: Path,
        screenplay: dict,
        aspect_ratio: str = "16:9",
        image_size: str = "1K",
        force_regenerate: bool = False,
        visual_style: str = ""
    ) -> dict:

        project_path = Path(project_path)

        asset_bible_path = project_path / "asset_bible.json"

        if not asset_bible_path.exists():
            raise RuntimeError(
                "Asset bible is missing. Generate references first."
            )

        asset_bible = self.read_json(asset_bible_path)

        if not asset_bible:
            raise RuntimeError("Asset bible is invalid")

        manifest = self.get_manifest(project_path)

        references = manifest.get("references", {})

        # Require all master references to be generated.
        for group in ("characters", "props", "locations"):
            for asset_id, record in references.get(group, {}).items():
                path = project_path / record.get("path", "")

                if (
                    record.get("status") != "completed"
                    or not self.valid_image(path)
                ):
                    raise RuntimeError(
                        f"Reference image is not ready: {asset_id}"
                    )

        (project_path / "scenes").mkdir(
            parents=True,
            exist_ok=True
        )

        scenes_manifest = manifest.setdefault("scenes", {})

        manifest["status"] = "generating_scenes"
        self.save_manifest(project_path, manifest)

        try:
            for scene in screenplay.get("scenes", []):

                scene_id = scene["scene_id"]

                output_path = (
                    project_path / "scenes" /
                    (self.safe_filename(scene_id) + ".png")
                )

                record = scenes_manifest.setdefault(
                    scene_id,
                    {
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "status": "pending"
                    }
                )

                if (
                    not force_regenerate
                    and self.valid_image(output_path)
                ):
                    record["status"] = "completed"
                    record["path"] = self.relative_path(
                        project_path, output_path
                    )
                    self.save_manifest(project_path, manifest)
                    continue

                mapping = next(
                    (
                        item for item in asset_bible["scene_assets"]
                        if item["scene_id"] == scene_id
                    ),
                    {}
                )

                character_ids = scene.get("characters", [])
                prop_ids = mapping.get("prop_ids", [])
                location_ids = mapping.get("location_ids", [])

                reference_paths = []

                # Model supports up to 4 character references
                # and 10 object references in this workflow.
                for character_id in character_ids[:4]:

                    record_ref = references.get(
                        "characters", {}
                    ).get(character_id)

                    if record_ref:
                        reference_paths.append(
                            project_path / record_ref["path"]
                        )

                object_ids = prop_ids + location_ids

                for asset_id in object_ids[:10]:

                    record_ref = (
                        references.get("props", {}).get(asset_id)
                        or references.get("locations", {}).get(asset_id)
                    )

                    if record_ref:
                        reference_paths.append(
                            project_path / record_ref["path"]
                        )

                prompt = self.scene_image_prompt(
                    scene,
                    screenplay,
                    asset_bible,
                    aspect_ratio,
                    visual_style
                )

                record["status"] = "running"
                record["reference_count"] = len(reference_paths)
                self.save_manifest(project_path, manifest)

                try:
                    result = await self.create_image(
                        prompt,
                        output_path,
                        reference_paths,
                        aspect_ratio,
                        image_size,
                        visual_style
                    )

                    record.update({
                        "status": "completed",
                        "path": self.relative_path(
                            project_path, output_path
                        ),
                        "reference_count": len(reference_paths),
                        "model": result["model"]
                    })

                except Exception as exc:
                    record.update({
                        "status": "failed",
                        "error": str(exc)
                    })

                    self.update_statistics(manifest)
                    self.save_manifest(project_path, manifest)
                    raise

                self.update_statistics(manifest)
                self.save_manifest(project_path, manifest)

            manifest["status"] = "completed"
            self.update_statistics(manifest)
            self.save_manifest(project_path, manifest)

            return {
                "success": True,
                "manifest": str(
                    project_path / "image_manifest.json"
                ),
                "statistics": manifest["statistics"]
            }

        except Exception:
            manifest["status"] = "failed"
            self.update_statistics(manifest)
            self.save_manifest(project_path, manifest)
            logger.exception("Scene image generation failed")
            raise

    # ---------------------------------------------------------
    # IMAGE STATUS
    # ---------------------------------------------------------

    def get_image_status(self, project_path: Path) -> dict:

        project_path = Path(project_path)

        manifest = self.get_manifest(project_path)

        self.update_statistics(manifest)

        return {
            "status": manifest.get("status", "pending"),
            "references": manifest.get("references", {}),
            "scenes": manifest.get("scenes", {}),
            "statistics": manifest.get("statistics", {})
        }
