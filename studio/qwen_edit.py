"""Qwen-Image-Edit costume workflow — loads the verified API workflow template.

Uses ``workflows/qwen-image-edit-api.json`` (the v16 uncensored safetensors model
with FluxKontext reference-latent handling, verified to produce good edits) and
parameterizes it: source image (LoadImage node 41) + edit prompt (node 68).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Node ids in the qwen-image-edit-api.json API workflow.
NODE_LOAD_IMAGE = "41"
NODE_PROMPT = "68"
NODE_SEED = "65"          # KSampler


def load_qwen_edit_workflow(root: Path | str) -> dict[str, Any]:
    """Load the qwen-image-edit API workflow from the project workflows dir."""
    wf_path = Path(root) / "workflows" / "qwen-image-edit-api.json"
    return json.loads(wf_path.read_text(encoding="utf-8"))


def adapt_qwen_edit_workflow(wf: dict[str, Any], prompt: str,
                             ref_image_filename: str, seed: int = 0,
                             steps: int = 8, cfg: float = 4.0) -> dict[str, Any]:
    """Set the edit prompt + source image on a copy of the API workflow.

    ``seed=0`` means randomize (the KSampler seed is set to a fresh random value).
    Defaults tuned for the v16 edit model: 8 steps, cfg 4.0.
    """
    import copy
    import random
    out = copy.deepcopy(wf)
    out[NODE_LOAD_IMAGE]["inputs"]["image"] = ref_image_filename
    out[NODE_PROMPT]["inputs"]["prompt"] = prompt
    if NODE_SEED in out and "seed" in out[NODE_SEED]["inputs"]:
        out[NODE_SEED]["inputs"]["seed"] = (random.randrange(2**63)
                                            if seed == 0 else seed)
    if NODE_SEED in out:
        out[NODE_SEED]["inputs"]["steps"] = steps
        out[NODE_SEED]["inputs"]["cfg"] = cfg
    return out
