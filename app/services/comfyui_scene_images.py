"""Dynamic scene-image generation through the project's generic ComfyUI API workflow.

The screenplay and image manifest supply all scene-specific values. Workflow node IDs
are discovered from the API graph at runtime; no character, scene, or filename is
embedded in this module.
"""
import asyncio
import copy
import logging
import random
import re
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class ComfyUISceneImageGenerator:
    def __init__(self):
        self.base_url = settings.comfyui_base_url.rstrip("/")
        self.workflow_path = Path(settings.comfyui_image_workflow_json)
        self.timeout = max(60, int(settings.comfyui_image_timeout_seconds))

    @staticmethod
    def _link(value):
        if isinstance(value, list) and len(value) >= 2:
            return str(value[0]), int(value[1])
        return None

    def load_workflow(self) -> dict:
        if not self.workflow_path.is_file():
            raise FileNotFoundError(
                "ComfyUI scene-image API workflow not found: "
                f"{self.workflow_path}. Export the generic workflow using "
                "ComfyUI > File > Export (API) and configure "
                "COMFYUI_IMAGE_WORKFLOW_JSON."
            )
        import json
        workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("ComfyUI image workflow is empty or invalid")
        return copy.deepcopy(workflow)

    @staticmethod
    def _find_one(workflow: dict, class_type: str) -> tuple[str, dict]:
        matches = [
            (str(node_id), node)
            for node_id, node in workflow.items()
            if isinstance(node, dict) and node.get("class_type") == class_type
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one {class_type} node; found {len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _set_link(node: dict, input_name: str, value):
        node.setdefault("inputs", {})[input_name] = value

    @classmethod
    def _find_adapter_chain(cls, workflow: dict, sampler_id: str, sampler: dict):
        """Return IPAdapterAdvanced nodes in model-flow order, root to sampler."""
        consumer_id = sampler_id
        link = cls._link(sampler.get("inputs", {}).get("model"))
        backwards = []
        visited = set()

        while link:
            source_id, _ = link
            if source_id in visited:
                raise ValueError("Cycle detected in ComfyUI model connection chain")
            visited.add(source_id)
            source = workflow.get(source_id)
            if not source or source.get("class_type") != "IPAdapterAdvanced":
                break
            backwards.append((source_id, consumer_id))
            link = cls._link(source.get("inputs", {}).get("model"))
            consumer_id = source_id

        backwards.reverse()
        return backwards

    async def _upload_image(self, client: httpx.AsyncClient, path: Path) -> str:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Reference image does not exist: {path}")
        with path.open("rb") as stream:
            response = await client.post(
                f"{self.base_url}/upload/image",
                data={"type": "input", "overwrite": "true"},
                files={"image": (path.name, stream, "application/octet-stream")},
            )
        response.raise_for_status()
        data = response.json()
        uploaded_name = data.get("name")
        if not uploaded_name:
            raise RuntimeError(f"ComfyUI upload returned no image name: {data}")
        subfolder = data.get("subfolder", "")
        return f"{subfolder}/{uploaded_name}" if subfolder else uploaded_name

    @staticmethod
    def _dimensions(aspect_ratio: str) -> tuple[int, int]:
        try:
            width_ratio, height_ratio = (
                float(part) for part in str(aspect_ratio).split(":", 1)
            )
            if width_ratio <= 0 or height_ratio <= 0:
                raise ValueError
        except (TypeError, ValueError):
            width_ratio, height_ratio = 16.0, 9.0

        if abs(width_ratio / height_ratio - 1.0) < 0.03:
            return 512, 512

        long_side = 640
        if width_ratio >= height_ratio:
            width = long_side
            height = round((long_side * height_ratio / width_ratio) / 8) * 8
        else:
            height = long_side
            width = round((long_side * width_ratio / height_ratio) / 8) * 8
        return max(256, width), max(256, height)

    def _prepare_workflow(
        self,
        workflow: dict,
        prompt: str,
        negative_prompt: str,
        uploaded_references: list[str],
        scene_id: str,
        aspect_ratio: str = "16:9",
    ) -> dict:
        sampler_id, sampler = self._find_one(workflow, "KSampler")

        positive_link = self._link(sampler.get("inputs", {}).get("positive"))
        negative_link = self._link(sampler.get("inputs", {}).get("negative"))
        if not positive_link or not negative_link:
            raise ValueError("KSampler positive/negative conditioning is not connected")

        for link, text_value, label in (
            (positive_link, prompt, "positive"),
            (negative_link, negative_prompt, "negative"),
        ):
            node = workflow.get(link[0])
            if not node or node.get("class_type") != "CLIPTextEncode":
                raise ValueError(f"KSampler {label} input must connect to CLIPTextEncode")
            node.setdefault("inputs", {})["text"] = text_value

        chain = self._find_adapter_chain(workflow, sampler_id, sampler)
        if len(uploaded_references) > len(chain):
            raise ValueError(
                f"Workflow has {len(chain)} IPAdapter reference slots, "
                f"but this scene selected {len(uploaded_references)} references."
            )

        for index, (adapter_id, consumer_id) in enumerate(chain):
            adapter = workflow[adapter_id]
            image_link = self._link(adapter.get("inputs", {}).get("image"))
            enabled_image = uploaded_references[index] if index < len(uploaded_references) else None

            if enabled_image:
                if not image_link:
                    raise ValueError(f"IPAdapter node {adapter_id} has no image input connection")
                loader_id, _ = image_link
                loader = workflow.get(loader_id)
                if not loader or loader.get("class_type") != "LoadImage":
                    raise ValueError(
                        f"IPAdapter node {adapter_id} image input must connect to LoadImage"
                    )
                loader.setdefault("inputs", {})["image"] = enabled_image
            else:
                # Bypass an unused adapter by connecting its consumer to the
                # adapter's original model input. Its LoadImage is then unreachable.
                original_model = adapter.get("inputs", {}).get("model")
                if not self._link(original_model):
                    raise ValueError(f"Cannot bypass IPAdapter node {adapter_id}: model input is missing")
                workflow[consumer_id].setdefault("inputs", {})["model"] = original_model

        sampler.setdefault("inputs", {})["seed"] = random.randint(0, 2**53 - 1)

        latent_nodes = [
            node for node in workflow.values()
            if node.get("class_type") == "EmptyLatentImage"
        ]
        if len(latent_nodes) > 1:
            raise ValueError("Expected at most one EmptyLatentImage node")
        if latent_nodes:
            width, height = self._dimensions(aspect_ratio)
            latent_nodes[0].setdefault("inputs", {}).update({
                "width": width, "height": height, "batch_size": 1
            })

        safe_scene = re.sub(r"[^\\w.-]+", "_", str(scene_id)).strip("._") or "scene"
        for node in workflow.values():
            if node.get("class_type") == "SaveImage":
                node.setdefault("inputs", {})["filename_prefix"] = f"HindiStoryStudio/{safe_scene}"

        return workflow

    async def _wait_for_image(self, client: httpx.AsyncClient, prompt_id: str) -> bytes:
        deadline = asyncio.get_running_loop().time() + self.timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await client.get(f"{self.base_url}/history/{prompt_id}")
            response.raise_for_status()
            history = response.json()
            entry = history.get(prompt_id, history) if isinstance(history, dict) else {}
            status = entry.get("status", {}) if isinstance(entry, dict) else {}
            if isinstance(status, dict) and status.get("status_str") == "error":
                raise RuntimeError(
                    f"ComfyUI image job failed: {status.get('messages', status)}"
                )
            outputs = entry.get("outputs", {}) if isinstance(entry, dict) else {}

            for node_output in outputs.values():
                for image_info in node_output.get("images", []):
                    params = {
                        "filename": image_info["filename"],
                        "subfolder": image_info.get("subfolder", ""),
                        "type": image_info.get("type", "output"),
                    }
                    image_response = await client.get(
                        f"{self.base_url}/view?{urlencode(params)}"
                    )
                    image_response.raise_for_status()
                    return image_response.content

            await asyncio.sleep(2)

        raise TimeoutError(
            f"ComfyUI did not produce an output image within {self.timeout} seconds "
            f"(prompt_id={prompt_id})"
        )

    async def generate(
        self,
        prompt: str,
        output_path: Path,
        reference_images: list[Path],
        scene_id: str,
        aspect_ratio: str = "16:9",
        negative_prompt: str = (
            "blurry, low quality, distorted anatomy, deformed body, extra limbs, "
            "duplicate characters, merged characters, missing characters, "
            "wrong identity, text, watermark"
        ),
    ) -> dict:
        workflow = self.load_workflow()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
            uploaded = []
            for image_path in reference_images:
                uploaded.append(await self._upload_image(client, Path(image_path)))

            workflow = self._prepare_workflow(
                workflow, prompt, negative_prompt, uploaded, scene_id, aspect_ratio
            )
            response = await client.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": settings.comfyui_client_id},
            )
            response.raise_for_status()
            queued = response.json()
            prompt_id = queued.get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"ComfyUI did not return prompt_id: {queued}")
            if queued.get("node_errors"):
                raise RuntimeError(f"ComfyUI rejected workflow: {queued['node_errors']}")

            image_bytes = await self._wait_for_image(client, prompt_id)
            temporary = output_path.with_suffix(output_path.suffix + ".tmp")
            temporary.write_bytes(image_bytes)
            temporary.replace(output_path)

        return {
            "model": "ComfyUI",
            "prompt_id": prompt_id,
            "reference_count": len(reference_images),
        }
