from app.services.image_generation import ImageGenerationService


def _sample_screenplay():
    return {
        "characters": [
            {"character_id": "CHAR_001", "name": "Traveler"}
        ],
        "scenes": [
            {"scene_id": "SCENE_001", "characters": ["CHAR_001"]},
            {"scene_id": "SCENE_002", "characters": ["CHAR_001"]},
        ],
    }


def _sample_asset_bible():
    return {
        "props": [
            {
                "prop_id": "PROP_001",
                "name": "Portable device",
                "description": "A small handheld device",
                "visual_lock": "Rectangular, dark metal body",
            }
        ],
        "locations": [
            {
                "location_id": "LOC_001",
                "name": "Interior",
                "description": "A simple interior",
                "visual_lock": "Stable room layout and architecture",
            }
        ],
        "scene_assets": [
            {
                "scene_id": "SCENE_001",
                "prop_ids": ["PROP_001"],
                "location_ids": ["LOC_001"],
            },
            {
                "scene_id": "SCENE_002",
                "prop_ids": ["PROP_001"],
                "location_ids": ["LOC_001"],
            },
        ],
        "asset_states": [
            {
                "scene_id": "SCENE_001",
                "entities": [
                    {
                        "entity_id": "CHAR_001",
                        "entity_type": "character",
                        "state": "The traveler has their usual appearance.",
                        "transition_from_previous": "Initial appearance.",
                    },
                    {
                        "entity_id": "PROP_001",
                        "entity_type": "prop",
                        "state": "The device is switched off and undamaged.",
                        "transition_from_previous": "Initial appearance.",
                    },
                    {
                        "entity_id": "LOC_001",
                        "entity_type": "location",
                        "state": "The interior is in its established condition.",
                        "transition_from_previous": "Initial appearance.",
                    }
                ],
            },
            {
                "scene_id": "SCENE_002",
                "entities": [
                    {
                        "entity_id": "CHAR_001",
                        "entity_type": "character",
                        "state": "The traveler has their usual appearance.",
                        "transition_from_previous": "No change from the previous appearance.",
                    },
                    {
                        "entity_id": "PROP_001",
                        "entity_type": "prop",
                        "state": "The device is switched on; its outer appearance is unchanged.",
                        "transition_from_previous": "The device was switched on.",
                    },
                    {
                        "entity_id": "LOC_001",
                        "entity_type": "location",
                        "state": "The interior is in its established condition.",
                        "transition_from_previous": "No change from the previous scene.",
                    }
                ],
            },
        ],
    }


def test_asset_bible_accepts_generic_scene_state_changes():
    ImageGenerationService.validate_asset_bible(
        _sample_asset_bible(),
        _sample_screenplay(),
    )


def test_asset_bible_rejects_unknown_state_entity():
    data = _sample_asset_bible()
    data["asset_states"][1]["entities"][0]["entity_id"] = "UNKNOWN_999"

    try:
        ImageGenerationService.validate_asset_bible(data, _sample_screenplay())
    except ValueError as exc:
        assert "Unknown entity ID" in str(exc)
    else:
        raise AssertionError("Unknown entity IDs must be rejected")


def test_scene_prompt_includes_state_snapshot_and_project_style():
    screenplay = _sample_screenplay()
    data = _sample_asset_bible()
    scene = {
        "scene_id": "SCENE_002",
        "summary": "The traveler uses the device.",
        "characters": ["CHAR_001"],
        "image_prompt": "A traveler holding the device.",
    }

    prompt = ImageGenerationService.scene_image_prompt(
        scene,
        screenplay,
        data,
        "16:9",
        "Photorealistic live-action cinematic",
    )

    assert "The device is switched on" in prompt
    assert "Photorealistic live-action cinematic" in prompt
