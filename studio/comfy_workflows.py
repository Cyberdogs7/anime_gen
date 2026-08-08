"""Krea 2 workflow loading + parameterization (DESIGN.md §10.1).

Loads the exact exported API workflow (`workflows/image_keyframe.json`), sets
the dynamic values, and runs a t2i generation through a ComfyClient. The LLM
'Refine Prompt' node (TextGenerate) and the LoRA switch are turned OFF so the
prompt we compile is exactly what the model conditions on - deterministic, no
LLM in the loop.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .clients.comfy import ComfyClient

# ResolutionSelector aspect labels (must match the node's choices exactly).
ASPECT_LABELS = {
    "1:1": "1:1 (Square)",
    "2:3": "2:3 (Portrait Photo)",
    "3:2": "3:2 (Photo)",
    "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
}

#: workflow node ids (fixed by the exported JSON)
NODE_PROMPT = "30:19"        # PrimitiveStringMultiline - the user prompt
NODE_SEED = "30:3"          # KSampler (also carries the seed)
NODE_ASPECT = "49"           # ResolutionSelector
NODE_REFINE = "30:24"        # Boolean (Refine Prompt?)
NODE_LORA_SWITCH = "30:23"   # Boolean (Enable LoRA?)
NODE_MODEL_SWITCH = "30:22"  # model ComfySwitchNode
NODE_SAVE = "29"             # SaveImage


def load_workflow(path: Path | str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def parameterize_keyframe(
    workflow: dict[str, Any],
    prompt: str,
    seed: int,
    aspect_ratio: str = "16:9",
    megapixels: float = 1.0,
    use_lora: bool = False,
) -> dict[str, Any]:
    """Set the dynamic values on a copy of the workflow. Returns the new dict."""
    import copy
    wf = copy.deepcopy(workflow)
    wf[NODE_PROMPT]["inputs"]["value"] = prompt
    wf[NODE_SEED]["inputs"]["seed"] = seed
    wf[NODE_ASPECT]["inputs"]["aspect_ratio"] = ASPECT_LABELS.get(aspect_ratio, aspect_ratio)
    wf[NODE_ASPECT]["inputs"]["megapixels"] = megapixels
    wf[NODE_REFINE]["inputs"]["value"] = False            # never LLM-enhance
    wf[NODE_LORA_SWITCH]["inputs"]["value"] = bool(use_lora)
    return wf


def run_t2i(client: ComfyClient, workflow: dict[str, Any], out_path: Path | str,
            timeout_s: float = 900.0) -> Path:
    """Submit the workflow, wait, and save the generated image to out_path."""
    prompt_id = client.submit(workflow)
    entry = client.wait(prompt_id, timeout_s=timeout_s)
    images = []
    for node_id, output in (entry.get("outputs") or {}).items():
        if "images" in output:
            images.extend(output["images"])
    if not images:
        raise RuntimeError(f"no image output for prompt {prompt_id}")
    img = images[0]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    client.download(img["filename"], subfolder=img.get("subfolder", ""),
                    node_type=img.get("type", "output"), dest=str(out))
    return out


def generate_keyframe(client: ComfyClient, workflow: dict[str, Any], prompt: str,
                      seed: int, out_path: Path | str, aspect_ratio: str = "16:9",
                      megapixels: float = 1.0, use_lora: bool = False) -> Path:
    wf = parameterize_keyframe(workflow, prompt, seed, aspect_ratio, megapixels, use_lora)
    return run_t2i(client, wf, out_path)


def build_keyframe_ref_workflow(workflow: dict[str, Any], prompt: str, seed: int,
                                aspect_ratio: str, ref_filenames: list[str],
                                weight: float = 0.8) -> dict[str, Any]:
    """Base keyframe workflow + an IPAdapter pass over the reference images.

    Routes the diffusion model through IPAdapterUnifiedLoader + IPAdapterAdvanced
    so the generated keyframe inherits the referenced character designs.
    """
    import copy
    wf = copy.deepcopy(workflow)
    wf[NODE_PROMPT]["inputs"]["value"] = prompt
    wf[NODE_SEED]["inputs"]["seed"] = seed
    wf[NODE_ASPECT]["inputs"]["aspect_ratio"] = ASPECT_LABELS.get(aspect_ratio, aspect_ratio)
    wf[NODE_ASPECT]["inputs"]["megapixels"] = 1.0
    wf[NODE_REFINE]["inputs"]["value"] = False
    wf[NODE_LORA_SWITCH]["inputs"]["value"] = False

    img_links = []
    for i, name in enumerate(ref_filenames):
        nid = f"refimg_{i}"
        wf[nid] = {"inputs": {"image": name, "upload": "image"},
                   "class_type": "LoadImage", "_meta": {"title": f"Ref {i}"}}
        img_links.append([nid, 0])

    wf["ipad_loader"] = {
        "inputs": {"model": [NODE_MODEL_SWITCH, 0], "preset": "PLUS (high strength)"},
        "class_type": "IPAdapterUnifiedLoader", "_meta": {"title": "IPAdapter Load"}}

    # One IPAdapter pass PER reference, chained: each applies a single character's
    # design onto the model in sequence (the reliable way to combine multiple
    # subjects; a batched IPAdapter input is flaky on this stack).
    prev_model: Any = [NODE_MODEL_SWITCH, 0]
    for i, link in enumerate(img_links):
        apply_id = f"ipad_apply_{i}"
        wf[apply_id] = {
            "inputs": {
                "model": prev_model,
                "ipadapter": ["ipad_loader", 1],
                "image": link,
                "weight": weight,
                "weight_type": "linear",
                "combine_embeds": "concat",
                "start_at": 0.0,
                "end_at": 1.0,
                "embeds_scaling": "V only",
            },
            "class_type": "IPAdapterAdvanced", "_meta": {"title": f"IPAdapter Apply {i}"}}
        prev_model = [apply_id, 0]
    wf[NODE_SEED]["inputs"]["model"] = prev_model
    return wf


def generate_keyframe_with_ref(client: ComfyClient, workflow: dict[str, Any], prompt: str,
                               seed: int, ref_image_paths: list[str],
                               out_path: Path | str, aspect_ratio: str = "16:9",
                               weight: float = 0.8) -> Path:
    """Upload the reference images, build the IPAdapter workflow, render."""
    filenames = [client.upload_image(p) for p in ref_image_paths]
    wf = build_keyframe_ref_workflow(workflow, prompt, seed, aspect_ratio, filenames, weight)
    return run_t2i(client, wf, out_path)
