import json
import uuid
from pathlib import Path
import httpx
from app.config import settings

class ComfyUIClient:
    """ComfyUI API client. Workflow must be exported using Save (API Format)."""
    def __init__(self):
        self.base = settings.comfyui_base_url.rstrip("/")
        self.client_id = settings.comfyui_client_id

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self.base}/system_stats")
            r.raise_for_status()
            return r.json()

    def load_workflow(self) -> dict:
        path = settings.comfyui_workflow_json
        if not path.exists():
            raise FileNotFoundError(f"ComfyUI API workflow not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def queue_workflow(self, workflow: dict) -> str:
        payload = {"prompt": workflow, "client_id": self.client_id}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{self.base}/prompt", json=payload)
            r.raise_for_status()
            return r.json()["prompt_id"]

    async def history(self, prompt_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self.base}/history/{prompt_id}")
            r.raise_for_status()
            return r.json()

    @staticmethod
    def set_node_input(workflow: dict, node_id: str, input_name: str, value):
        if node_id not in workflow:
            raise KeyError(f"Workflow node ID {node_id} not found")
        workflow[node_id].setdefault("inputs", {})[input_name] = value
        return workflow

    async def queue_ltx_scene(self, image_filename: str, audio_filename: str,
                              prompt: str, node_map: dict) -> str:
        workflow = self.load_workflow()
        # node_map example: {"image":{"id":"12","input":"image"},
        # "audio":{"id":"18","input":"audio"}, "prompt":{"id":"5","input":"text"}}
        for key, value in (("image", image_filename), ("audio", audio_filename), ("prompt", prompt)):
            mapping = node_map[key]
            self.set_node_input(workflow, str(mapping["id"]), mapping["input"], value)
        return await self.queue_workflow(workflow)
