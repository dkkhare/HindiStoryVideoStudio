# ComfyUI workflows

## Automatic scene-image generation

The repository includes `automatic_reference_api.json`, an API-format SD 1.5
workflow used by `ComfyUISceneImageGenerator`.

It contains five ordered IPAdapter reference slots:

1. Character 1
2. Character 2
3. Character 3
4. Character 4
5. Location

The application fills character slots by the screenplay's character order and
always reserves slot 5 for the location reference. Empty slots are bypassed by
the adapter so scenes with fewer characters, no location reference, or no
references can still run.

### Required ComfyUI assets

- SD 1.5 checkpoint: `v1-5-pruned-emaonly-fp16.safetensors`
- ComfyUI IPAdapter Plus custom nodes, including `IPAdapterUnifiedLoader`
  and `IPAdapterAdvanced`
- The IPAdapter model and CLIP Vision assets required by the selected
  `PLUS (high strength)` preset

Install the required custom nodes and model assets in the ComfyUI instance
configured by `COMFYUI_BASE_URL`. The application uploads reference images
through ComfyUI's `/upload/image` endpoint, queues the modified API workflow
through `/prompt`, polls `/history/{prompt_id}`, and downloads the result
through `/view`.

Configure:

```dotenv
COMFYUI_IMAGE_WORKFLOW_JSON=./workflows/automatic_reference_api.json
SCENE_IMAGE_PROVIDER=comfyui
COMFYUI_IMAGE_TIMEOUT_SECONDS=900
```

## LTX video workflow

Export the actual LTX-2.5 graph using ComfyUI's **Save (API Format)** option
and place it at `ltx25_i2v_api.json`. Its node IDs and input names must be
mapped to the video adapter separately; do not reuse the SD 1.5 image workflow
for video generation.
