import json
import unittest
from pathlib import Path

from app.services.comfyui_scene_images import ComfyUISceneImageGenerator


def sample_workflow():
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
        "2": {"class_type": "IPAdapterUnifiedLoader", "inputs": {"model": ["1", 0]}},
        "3": {"class_type": "LoadImage", "inputs": {"image": "old-character-a.png"}},
        "4": {"class_type": "IPAdapterAdvanced", "inputs": {"model": ["2", 0], "image": ["3", 0]}},
        "5": {"class_type": "LoadImage", "inputs": {"image": "old-character-b.png"}},
        "6": {"class_type": "IPAdapterAdvanced", "inputs": {"model": ["4", 0], "image": ["5", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "old positive"}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": "old negative"}},
        "9": {"class_type": "KSampler", "inputs": {
            "model": ["6", 0], "positive": ["7", 0], "negative": ["8", 0],
            "seed": 1,
        }},
        "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "old"}},
    }


class DynamicComfyWorkflowTests(unittest.TestCase):
    def test_unused_adapter_is_bypassed_and_prompt_is_dynamic(self):
        workflow = sample_workflow()
        result = ComfyUISceneImageGenerator()._prepare_workflow(
            workflow,
            prompt="A new screenplay scene",
            negative_prompt="blur",
            uploaded_references=["upload/CHAR_A.png"],
            scene_id="SCENE_042",
        )

        self.assertEqual(result["3"]["inputs"]["image"], "upload/CHAR_A.png")
        self.assertEqual(result["9"]["inputs"]["model"], ["4", 0])
        self.assertEqual(result["7"]["inputs"]["text"], "A new screenplay scene")
        self.assertEqual(result["8"]["inputs"]["text"], "blur")
        self.assertEqual(result["10"]["inputs"]["filename_prefix"], "HindiStoryStudio/SCENE_042")

    def test_all_adapters_are_bypassed_for_text_only_scene(self):
        workflow = sample_workflow()
        result = ComfyUISceneImageGenerator()._prepare_workflow(
            workflow,
            prompt="Forest at sunrise",
            negative_prompt="",
            uploaded_references=[],
            scene_id="SCENE_001",
        )

        self.assertEqual(result["9"]["inputs"]["model"], ["2", 0])
        self.assertEqual(result["7"]["inputs"]["text"], "Forest at sunrise")


    def test_exported_workflow_preserves_character_and_location_slots(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "workflows"
            / "automatic_reference_api.json"
        )
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))

        result = ComfyUISceneImageGenerator()._prepare_workflow(
            workflow,
            prompt="A grandmother telling a story to a child",
            negative_prompt="blur",
            uploaded_references=[
                "input/CHAR_001.png",
                None,
                None,
                None,
                "input/LOCATION.png",
            ],
            scene_id="SCENE_007",
            aspect_ratio="16:9",
        )

        self.assertEqual(result["20"]["inputs"]["image"], "input/CHAR_001.png")
        self.assertEqual(result["28"]["inputs"]["image"], "input/LOCATION.png")
        self.assertEqual(result["22"]["inputs"]["image"], "CHAR_002.png")
        self.assertEqual(result["9"]["inputs"]["text"], "A grandmother telling a story to a child")
        self.assertEqual(result["10"]["inputs"]["text"], "blur")
        self.assertEqual(result["11"]["inputs"]["width"], 640)
        self.assertEqual(result["11"]["inputs"]["height"], 360)
        self.assertEqual(result["13"]["inputs"]["filename_prefix"], "HindiStoryStudio/SCENE_007")

    def test_exported_workflow_bypasses_all_five_empty_adapters(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "workflows"
            / "automatic_reference_api.json"
        )
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))

        result = ComfyUISceneImageGenerator()._prepare_workflow(
            workflow,
            prompt="A quiet forest at sunrise",
            negative_prompt="",
            uploaded_references=[None, None, None, None, None],
            scene_id="SCENE_008",
        )

        self.assertEqual(result["3"]["inputs"]["model"], ["1", 0])
        self.assertEqual(result["9"]["inputs"]["text"], "A quiet forest at sunrise")



if __name__ == "__main__":
    unittest.main()
