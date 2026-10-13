"""MiniMax H3 Multishot seamless chaining (joeygambino v2.6.3) - corrected.

Uses the pack's actual nodes:
- H3ModelLoaderAny (safetensors or GGUF)
- H3ClipLoaderAny (type=minimax)
- VAELoader (video + audio)
- H3MultishotSampler (continuity=first_frame, CORE, no extra deps)
- H3MultishotMemorySampler (continuity=context_pin, needs ComfyUI-H3-Motion-Context)
-> CreateVideo (fps 24) -> SaveVideo + SaveAudio

Example workflow: workflows/H3_Seamless_Chain_CORE.json in the pack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .clients.comfy import ComfyClient
from .config import get_config


MULTISHOT_SAMPLER_FIRST = "H3MultishotSampler"
MULTISHOT_SAMPLER_MEMORY = "H3MultishotMemorySampler"


def build_h3_multishot_workflow(
    prompt_blocks: list[str],
    seed: int = 1,
    cfg=None,
    continuity: str = "context_pin",
    frames_per_shot: int = 192,
    width: int = 1280,
    height: int = 736,
    steps: int = 14,
    sampler_name: str = "euler",
    scheduler: str = "beta",
    chain_gain_control: str = "flatten",
    master_normalize: str = "luma+contrast",
    seed_per_shot: bool = True,
    preview_first_shot: bool = True,
    low_ram_master: bool = False,
    ref_image_filenames: list[str] | None = None,
    ref_audio_filenames: list[str] | None = None,
    take_seconds: int = 0,
) -> dict[str, Any]:
    """Build a ComfyUI graph that renders a scene as one multishot master.

    prompt_blocks: one H3 prompt per shot, joined with ---.
    continuity: "first_frame" -> H3MultishotSampler, "context_pin" -> H3MultishotMemorySampler
    frames_per_shot: 192=8s (pack default), 243=10.1s, must be 5 mod 17 on H3 grid
    """
    cfg = cfg or get_config()
    ck = cfg.get("comfy", "checkpoints", {})

    # Resolve model/clip/vae names - prefer GGUF-aware loader names if present
    # H3ModelLoaderAny handles both safetensors and GGUF; feed whatever is configured.
    model_name = ck.get("h3_ref2va") or ck.get("h3_fl2va") or "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    clip_name = ck.get("h3_clip") or "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    video_vae_name = ck.get("h3_video_vae") or "minimax_h3_video_vae_fp16.safetensors"
    audio_vae_name = ck.get("h3_audio_vae") or "minimax_h3_audio_vae_fp32.safetensors"

    # Sampler choice
    use_memory = continuity == "context_pin"
    sampler_class = MULTISHOT_SAMPLER_MEMORY if use_memory else MULTISHOT_SAMPLER_FIRST

    script = "\n---\n".join(b.strip() for b in prompt_blocks if b and b.strip())
    # H3Multishot pack auto-windows take_seconds if >0, but we keep script as blocks.
    # take_seconds handling is a UI convenience in the pack's H3StudioControls; at
    # API level we just pass script + frames_per_shot and let the sampler chain.
    # If take_seconds>0 and only one block, the sampler will still chunk it.

    wf: dict[str, Any] = {}

    # Loaders
    wf["1"] = {"class_type": "H3ModelLoaderAny", "inputs": {"model_name": model_name}}
    wf["2"] = {"class_type": "H3ClipLoaderAny", "inputs": {"clip_name": clip_name, "type": "minimax"}}
    wf["3"] = {"class_type": "VAELoader", "inputs": {"vae_name": video_vae_name}}
    wf["4"] = {"class_type": "VAELoader", "inputs": {"vae_name": audio_vae_name}}

    # Optional reference images -> IMAGE batch for reference_images input
    # Pack's reference_subjects groups pictures by subject to prevent blending:
    # "1,1,1" = 3 subjects with 1 image each (one per referenced character).
    ref_images_input = None
    reference_subjects = None
    if ref_image_filenames:
        load_ids = []
        for i, fname in enumerate(ref_image_filenames):
            nid = f"rimg{i}"
            wf[nid] = {"class_type": "LoadImage", "inputs": {"image": fname}}
            load_ids.append(nid)
        if len(load_ids) == 1:
            ref_images_input = [load_ids[0], 0]
            reference_subjects = "1"
        else:
            prev = [load_ids[0], 0]
            for k in range(1, len(load_ids)):
                bid = f"rbatch{k}"
                wf[bid] = {"class_type": "ImageBatch", "inputs": {"image1": prev, "image2": [load_ids[k], 0]}}
                prev = [bid, 0]
            ref_images_input = prev
            # Only set when we have multiple subjects: 1 image per subject, comma-separated counts
            reference_subjects = ",".join(["1"] * len(load_ids))

    # Optional voice anchor -> AUDIO (global for the master)
    # Silent-window hallucination: if the scene is mostly silent, pinning a voice causes "Isisis" gibberish.
    # We keep it but the LLM must render silent windows with no <d> and lips closed. The sampler's fix
    # (v2.6.3) relies on prompt wording, not on omitting the anchor.
    voice_ref_input = None
    if ref_audio_filenames:
        fname = ref_audio_filenames[0]
        wf["ra"] = {"class_type": "LoadAudio", "inputs": {"audio": fname}}
        voice_ref_input = ["ra", 0]

    # Sampler — MemorySampler requires memory_frames/anchor_frames per pack spec
    sampler_inputs: dict[str, Any] = {
        "model": ["1", 0],
        "clip": ["2", 0],
        "video_vae": ["3", 0],
        "audio_vae": ["4", 0],
        "script": script,
        "shot_count": 0,  # 0 = one per --- block
        "width": int(width),
        "height": int(height),
        "frames_per_shot": int(frames_per_shot),
        "seed": int(seed),
        "steps": int(steps),
        "seed_per_shot": bool(seed_per_shot),
    }
    if use_memory:
        sampler_inputs["memory_frames"] = 0  # RECENCY slots, 0 = only pinned shot 1 (anti-drift)
        sampler_inputs["anchor_frames"] = 1  # identity refs from start_image, 1 = every shot sees shot 1
    # Optional pins — only H3MultishotSampler supports reference_subjects (v2.2.4 fix)
    if ref_images_input is not None:
        if use_memory:
            sampler_inputs["start_image"] = ref_images_input
        else:
            sampler_inputs["reference_images"] = ref_images_input
            if reference_subjects and not use_memory:
                # Pack's H3MultishotSampler field: "reference_subjects" = "1,1,1" etc.
                # Silently ignored if the sampler doesn't expose it (CORE without patch).
                sampler_inputs["reference_subjects"] = reference_subjects
    if voice_ref_input is not None:
        if use_memory:
            sampler_inputs["guide_audio"] = voice_ref_input
        else:
            sampler_inputs["voice_ref"] = voice_ref_input

    # Sampler-specific optional tuning is embedded in the node via its own UI;
    # we keep chain_gain_control/master_normalize as documentation only here
    # (they are properties of the node's internal logic, not API inputs).
    # sampler_name/scheduler are not direct inputs on the multishot samplers;
    # they are controlled via the underlying Diffusers scheduler, but the pack
    # exposes them via H3StudioControls overrides. For direct API we set them via
    # sampler_override/scheduler_override if provided.
    if sampler_name and sampler_name != "euler":
        sampler_inputs["sampler_name"] = sampler_name
    if scheduler and scheduler != "beta":
        sampler_inputs["scheduler"] = scheduler

    wf["10"] = {"class_type": sampler_class, "inputs": sampler_inputs}

    # Mux
    wf["11"] = {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "audio": ["10", 1], "fps": 24}}
    wf["12"] = {"class_type": "SaveVideo", "inputs": {"video": ["11", 0], "filename_prefix": "h3chain_master", "format": "mp4", "codec": "h264"}}
    # Also save audio track separately (pack does both)
    wf["13"] = {"class_type": "SaveAudio", "inputs": {"audio": ["10", 1], "filename_prefix": "h3chain_audio"}}

    return wf


def run_h3_multishot(
    client: ComfyClient,
    workflow: dict[str, Any],
    out_path: Path | str,
    timeout_s: float = 3600.0,
    hard_timeout_s: float | None = None,
) -> Path:
    """Submit a multishot workflow and download the master."""
    prompt_id = client.submit(workflow)
    entry = client.wait(prompt_id, timeout_s=timeout_s, hard_timeout_s=hard_timeout_s or timeout_s * 2)
    if entry.get("status", {}).get("status_str") == "error":
        msgs = entry.get("status", {}).get("messages", [])
        err = [m[1] for m in msgs if m[0] == "execution_error"]
        raise RuntimeError(f"multishot render failed: {err[0] if err else 'see ComfyUI console'}")
    videos = []
    for node_id, output in (entry.get("outputs") or {}).items():
        if "video" in output:
            videos.extend(output["video"] if isinstance(output["video"], list) else [output["video"]])
        if "gifs" in output:
            videos.extend(output["gifs"])
        if output.get("images") and output.get("animated"):
            videos.extend(output["images"])
    if not videos:
        raise RuntimeError(f"no video output for multishot prompt {prompt_id}")
    v = videos[0]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    client.download(v["filename"], subfolder=v.get("subfolder", ""), node_type=v.get("type", "output"), dest=str(out))
    return out
