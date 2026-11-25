"""Hosted ComfyUI API client for kImage.

The Qwen Image Edit encoder exposes three image sockets. To support an
open-ended number of scene references, this client builds balanced image
stitch trees and feeds up to three composite reference images to the encoder.
"""
import asyncio
import json
import uuid
from pathlib import Path

import httpx


class KImageComfyAPI:
    def __init__(self, api_url: str, api_key: str = "", workflow_json: str = ""):
        self.base_url = (api_url or "").rstrip("/")
        self.api_key = api_key or ""
        if not self.base_url:
            raise ValueError("Configure the kImage ComfyUI API URL in Project API Settings.")
        try:
            self.workflow = json.loads(workflow_json) if isinstance(workflow_json, str) else workflow_json
        except json.JSONDecodeError as exc:
            raise ValueError("kImage workflow JSON is invalid.") from exc
        if not isinstance(self.workflow, dict) or not self.workflow:
            raise ValueError("Paste the API-exported Qwen Image Edit workflow in Project API Settings.")
        self.headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def _upload(self, client, path):
        path = Path(path)
        with path.open("rb") as fh:
            response = await client.post(
                f"{self.base_url}/upload/image",
                headers=self.headers,
                data={"overwrite": "true", "type": "input"},
                files={"image": (path.name, fh, "image/png")},
            )
        response.raise_for_status()
        data = response.json()
        name = data.get("name") or data.get("filename")
        if not name:
            raise RuntimeError(f"kImage upload returned no filename: {data}")
        subfolder = data.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    @staticmethod
    def _dimensions(ratio, size):
        base = 1024 if str(size).upper() == "1K" else 1536
        try:
            a, b = (float(x) for x in ratio.split(":"))
            scale = base / max(a, b)
            return max(64, int(a * scale // 64) * 64), max(64, int(b * scale // 64) * 64)
        except Exception:
            return 1280, 720

    @staticmethod
    def _next_node_id(workflow):
        numeric_ids = [int(key) for key in workflow if str(key).isdigit()]
        return str(max(numeric_ids, default=0) + 1)

    def _add_reference_tree(self, workflow, filenames, group_number):
        """Build a binary ImageStitch tree for one encoder image socket."""
        current = []
        for index, filename in enumerate(filenames):
            node_id = self._next_node_id(workflow)
            workflow[node_id] = {
                "class_type": "LoadImage",
                "inputs": {"image": filename},
                "_meta": {"title": f"kImage reference {index + 1} (group {group_number})"},
            }
            current.append((node_id, 0))

        while len(current) > 1:
            next_level = []
            iterator = iter(current)
            for left in iterator:
                right = next(iterator, None)
                if right is None:
                    next_level.append(left)
                    continue
                node_id = self._next_node_id(workflow)
                workflow[node_id] = {
                    "class_type": "ImageStitch",
                    "inputs": {
                        "direction": "right",
                        "match_image_size": True,
                        "spacing_width": 0,
                        "spacing_color": "white",
                        "image1": [left[0], left[1]],
                        "image2": [right[0], right[1]],
                    },
                    "_meta": {"title": f"kImage reference stitch {node_id}"},
                }
                next_level.append((node_id, 0))
            current = next_level
        return current[0]

    def _patch(self, prompt, refs, width, height):
        workflow = json.loads(json.dumps(self.workflow))
        encoders = []
        save_nodes = []

        for node_id, node in list(workflow.items()):
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", "")).lower()
            inputs = node.setdefault("inputs", {})
            if class_type == "textencodeqwenimageeditplus":
                encoders.append(node_id)
                for key in list(inputs):
                    if key.lower().startswith("image") and key[5:].isdigit():
                        inputs.pop(key, None)
                if "prompt" in inputs and not inputs["prompt"]:
                    inputs["prompt"] = ""
            for key, value in list(inputs.items()):
                lowered = str(key).lower()
                if lowered in {"width", "w"} and isinstance(value, (int, float)):
                    inputs[key] = width
                elif lowered in {"height", "h"} and isinstance(value, (int, float)):
                    inputs[key] = height
            if "saveimage" in class_type or "previewimage" in class_type:
                save_nodes.append(node_id)

        if not encoders:
            raise RuntimeError("The kImage workflow must contain TextEncodeQwenImageEditPlus.")
        if not save_nodes:
            raise RuntimeError("The kImage workflow has no SaveImage or PreviewImage output node.")

        # The positive encoder is normally the first node and contains the
        # non-empty prompt. Update it; retain the negative prompt unchanged.
        positive = next(
            (node_id for node_id in encoders if workflow[node_id]["inputs"].get("prompt")),
            encoders[0],
        )
        workflow[positive]["inputs"]["prompt"] = prompt

        # Remove stale reference loader/stitch nodes from an exported workflow.
        # They are rebuilt from the exact references needed by this scene.
        for node_id, node in list(workflow.items()):
            if node_id in encoders:
                continue
            class_type = str(node.get("class_type", "")).lower() if isinstance(node, dict) else ""
            if class_type in {"loadimage", "imagestitch"}:
                workflow.pop(node_id, None)

        if refs:
            # Qwen's encoder exposes three image sockets. Pack an arbitrary
            # number of references into three balanced composite images.
            groups = [[], [], []]
            for index, filename in enumerate(refs):
                groups[index % 3].append(filename)
            for socket_index, filenames in enumerate(groups, start=1):
                if not filenames:
                    continue
                source = self._add_reference_tree(workflow, filenames, socket_index)
                socket_name = f"image{socket_index}"
                for encoder_id in encoders:
                    workflow[encoder_id]["inputs"][socket_name] = [source[0], source[1]]

        return workflow

    async def generate_image(
        self, prompt, output_path, reference_images,
        aspect_ratio="16:9", image_size="1K"
    ):
        # Deduplicate paths while preserving order. This avoids uploading the
        # same asset twice when it is referenced by multiple screenplay roles.
        unique_paths = []
        seen = set()
        for item in reference_images or []:
            path = Path(item)
            key = str(path.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                unique_paths.append(path)

        missing = [str(path) for path in unique_paths if not path.is_file()]
        if missing:
            raise FileNotFoundError("Required kImage reference image(s) are missing: " + ", ".join(missing))

        async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=60)) as client:
            uploaded = [await self._upload(client, path) for path in unique_paths]
            width, height = self._dimensions(aspect_ratio, image_size)
            workflow = self._patch(prompt, uploaded, width, height)
            response = await client.post(
                f"{self.base_url}/prompt",
                headers=self.headers,
                json={"prompt": workflow, "client_id": str(uuid.uuid4())},
            )
            response.raise_for_status()
            prompt_id = response.json().get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"kImage /prompt returned no prompt_id: {response.text[:400]}")

            deadline = asyncio.get_running_loop().time() + 1800
            outputs = {}
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(2)
                poll = await client.get(
                    f"{self.base_url}/history/{prompt_id}", headers=self.headers
                )
                poll.raise_for_status()
                history = poll.json().get(prompt_id, {})
                outputs = history.get("outputs") or {}
                status = history.get("status") or {}
                if outputs:
                    break
                if isinstance(status, dict) and status.get("status_str") == "error":
                    raise RuntimeError(f"kImage execution failed: {status}")
            else:
                raise TimeoutError(f"kImage workflow {prompt_id} timed out after 30 minutes.")

            images = [
                image
                for node in outputs.values() if isinstance(node, dict)
                for image in node.get("images", [])
            ]
            if not images:
                raise RuntimeError(f"kImage workflow {prompt_id} completed without image output.")

            image = images[0]
            result = await client.get(
                f"{self.base_url}/view",
                headers=self.headers,
                params={
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                },
            )
            result.raise_for_status()
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_bytes(result.content)
            temporary.replace(output)

        return {"model": "kImage (ComfyUI)", "prompt_id": prompt_id}
