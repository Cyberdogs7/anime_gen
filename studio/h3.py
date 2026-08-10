"""MiniMax H3 shot generation via the ComfyUI Director node (DESIGN.md §10.3).

Builds the H3 Director API workflow programmatically from the node's real
schema, runs a t2v (fl2va) or ref2va shot, and saves the MP4.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .clients.comfy import ComfyClient
from .compile.durations import snap_duration
from .config import get_config


def build_timeline(global_prompt: str, frames: int, segment_prompt: str = "",
                   prompt_format: str = "MiniMax", reference_mode: str = "OFF",
                   characters: list[dict] | None = None,
                   audio_segments: list[dict] | None = None,
                   soundscape: str = "", music: str = "") -> str:
    tl: dict[str, Any] = {
        "mainTrackEnabled": True, "audioTrackEnabled": True, "motionTrackEnabled": True,
        "propHeight": 90, "globalPropHeight": 85,
        "showFilenames": True, "showPromptZones": True,
        "overrideAudio": False, "inpaint_audio": True,
        "global_prompt": global_prompt,
        "retake_global_prompt": "", "retakeMode": False,
        "retakeStart": 0, "retakeLength": 0, "retakePrompt": "", "retakeStrength": 1,
        "retakeVideo": None,
        "normalStartFrame": 0, "normalDurationFrames": frames,
        "reference_mode": reference_mode, "prompt_format": prompt_format,
        "analyzeProvider": "", "analyzeBaseUrl": "", "analyzeModel": "",
        "characters": characters or [],
        "segments": [{"id": "seg0", "start": 0, "length": frames,
                      "prompt": segment_prompt, "type": "text", "isEndFrame": False}],
        "motionSegments": [], "audioSegments": audio_segments or [],
        "overall_soundscape": soundscape or "", "non_diegetic_music": music or "",
    }
    return json.dumps(tl)


def _add_ref_images(wf: dict[str, Any], director_id: str, ref_images: list[str]) -> None:
    """Add LoadImage(+ImageBatch) nodes feeding the Director's ref_images socket.

    `ref_images` are filenames already present in ComfyUI's input directory.
    """
    load_ids = [f"ref{i}" for i in range(1, len(ref_images) + 1)]
    for nid, filename in zip(load_ids, ref_images):
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": filename}}
    if len(load_ids) == 1:
        source = [load_ids[0], 0]
    else:
        prev = [load_ids[0], 0]
        for k in range(1, len(load_ids)):
            batch_id = f"refb{k}"
            wf[batch_id] = {"class_type": "ImageBatch",
                            "inputs": {"image1": prev, "image2": [load_ids[k], 0]}}
            prev = [batch_id, 0]
        source = prev
    wf[director_id]["inputs"]["ref_images"] = source


def build_h3_shot_workflow(
    global_prompt: str,
    duration_s: float,
    seed: int,
    cfg=None,
    segment_prompt: str = "",
    characters: list[dict] | None = None,
    use_ref2va: bool = False,
    ref_images: list[str] | None = None,
    audio_segments: list[dict] | None = None,
    soundscape: str = "",
    music: str = "",
    use_spectrum: bool = False,
    use_first_block_cache: bool = False,
    steps: int = 4,
    fps: int = 24,
    width: int = 854,
    height: int = 480,
    sampler_name: str = "sa_solver",
    scheduler: str = "simple",
    use_turbo_lora: bool = True,
    lora_strength: float = 0.75,
) -> dict[str, Any]:
    cfg = cfg or get_config()
    ck = cfg.get("comfy", "checkpoints", {})
    k, frames, _ = snap_duration(duration_s)
    ref_images = ref_images or []
    audio_segments = audio_segments or []
    ref_mode = "REF2VA" if (use_ref2va or ref_images or audio_segments or characters) else "OFF"
    timeline = build_timeline(global_prompt, frames, segment_prompt=segment_prompt,
                              reference_mode=ref_mode, characters=characters,
                              audio_segments=audio_segments,
                              soundscape=soundscape, music=music)

    wf: dict[str, Any] = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": ck.get("h3_fl2va"), "weight_dtype": "default"}},
        "1a": {"class_type": "LoraLoaderModelOnly",
               "inputs": {"lora_name": ck.get("h3_turbo_lora"),
                          "strength_model": lora_strength, "model": ["1", 0]}},
        "2": {"class_type": "UNETLoader",
              "inputs": {"unet_name": ck.get("h3_ref2va"), "weight_dtype": "default"}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": ck.get("h3_clip"), "type": "minimax", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": ck.get("h3_video_vae")}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": ck.get("h3_audio_vae")}},
        "6": {"class_type": "MiniMaxH3DirectorCS",
              "inputs": {
                  "model": ["1a", 0], "model_ref2va": ["2", 0],
                  "clip": ["3", 0], "vae": ["4", 0], "audio_vae": ["5", 0],
                  "global_prompt": global_prompt,
                  "start_second": 0.0, "end_second": round(frames / fps, 3),
                  "duration_seconds": round(frames / fps, 3),
                  "start_frame": 0, "end_frame": frames, "duration_frames": frames,
                  "timeline_data": timeline,
                  "local_prompts": segment_prompt or global_prompt,
                  "segment_lengths": f"[{frames}]", "guide_strength": "",
                  "frame_rate": fps,
                  "custom_width": width, "custom_height": height,
                  "use_custom_audio": bool(audio_segments),
              }},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler_name}},
        "9": {"class_type": "BasicScheduler",
              "inputs": {"model": ["6", 0], "scheduler": scheduler, "steps": steps, "denoise": 1.0}},
        "10": {"class_type": "BasicGuider",
               "inputs": {"model": ["6", 0], "conditioning": ["6", 1]}},
        "11": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0],
                          "sigmas": ["9", 0], "latent_image": ["6", 2]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["4", 0]}},
        "13": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["11", 0], "vae": ["5", 0]}},
        "14": {"class_type": "CreateVideo",
               "inputs": {"images": ["12", 0], "audio": ["13", 0], "fps": ["6", 4]}},
        "15": {"class_type": "SaveVideo",
               "inputs": {"video": ["14", 0], "filename_prefix": "h3_shot",
                          "format": "mp4", "codec": "h264"}},
    }
    if ref_images:
        _add_ref_images(wf, "6", ref_images)
    if use_spectrum or use_first_block_cache:
        model_src: list[Any] = ["6", 0]
        if use_spectrum:
            wf["17"] = {"class_type": "SpectrumApplyMiniMaxH3",
                        "inputs": {"model": model_src, "enabled": True, "blend_weight": 0.5,
                                   "degree": 1, "ridge_lambda": 0.10, "window_size": 2.0,
                                   "flex_window": 0.75, "warmup_steps": 1, "tail_actual_steps": 1,
                                   "max_history": 8, "debug": False}}
            model_src = ["17", 0]
        if use_first_block_cache:
            wf["18"] = {"class_type": "ApplyMiniMaxH3FirstBlockCache",
                        "inputs": {"model": model_src,
                                   "mode": "H3 Fast — 0.10 / max 2",
                                   "threshold": 0.10, "start_percent": 0.10,
                                   "end_percent": 0.95, "max_consecutive_hits": 2,
                                   "temporal_guard": False}}
            model_src = ["18", 0]
        wf["9"]["inputs"]["model"] = model_src
        wf["10"]["inputs"]["model"] = model_src
    return wf


def build_h3_ref2va_workflow(
    prompt: str,
    duration_s: float,
    seed: int,
    cfg=None,
    ref_image_filenames: list[str] | None = None,
    ref_audio_filenames: list[str] | None = None,
    fps: int = 24,
    width: int = 1344,
    height: int = 768,
    steps: int = 8,
    sampler_name: str = "res_multistep",
    scheduler: str = "simple",
    use_spectrum: bool = False,
    use_first_block_cache: bool = False,
    use_turbo_lora: bool = True,
    lora_strength: float = 0.75,
) -> dict[str, Any]:
    """Official Comfy-Org ref2va workflow (comfy_extras MiniMaxH3ReferenceToVideo).

    Unlike the third-party Director wrapper, this is the core H3 graph: refs are
    wired as SOCKETS (LoadImage -> ref_image_0..8, LoadAudio -> ref_audio_0..2)
    and the node auto-labels them <Picture N> / <Audio N> in connection order,
    which the prompt references directly. Width/height default to H3's native
    1344x768 canvas.
    """
    cfg = cfg or get_config()
    ck = cfg.get("comfy", "checkpoints", {})
    k, frames, _ = snap_duration(duration_s)
    ref_image_filenames = ref_image_filenames or []
    ref_audio_filenames = ref_audio_filenames or []

    wf: dict[str, Any] = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": ck.get("h3_ref2va"),
                         "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": ck.get("h3_clip"), "type": "minimax",
                         "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": ck.get("h3_video_vae")}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": ck.get("h3_audio_vae")}},
    }

    # ref sockets (autogrow): LoadImage -> ref_image_N, LoadAudio -> ref_audio_N
    cond_inputs: dict[str, Any] = {
        "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
        "prompt": prompt,
        "width": int(width), "height": int(height), "length": int(frames),
        "ref_image_size": "match",
    }
    for i, fname in enumerate(ref_image_filenames):
        nid = f"img{i}"
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": fname}}
        cond_inputs[f"ref_images.ref_image_{i}"] = [nid, 0]
    for i, fname in enumerate(ref_audio_filenames):
        nid = f"aud{i}"
        wf[nid] = {"class_type": "LoadAudio", "inputs": {"audio": fname}}
        cond_inputs[f"ref_audios.ref_audio_{i}"] = [nid, 0]
    wf["5"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": cond_inputs}

    model_src: list[Any] = ["1", 0]
    if use_spectrum or use_first_block_cache:
        if use_spectrum:
            wf["17"] = {"class_type": "SpectrumApplyMiniMaxH3",
                        "inputs": {"model": model_src, "enabled": True, "blend_weight": 0.5,
                                   "degree": 1, "ridge_lambda": 0.10, "window_size": 2.0,
                                   "flex_window": 0.75, "warmup_steps": 1, "tail_actual_steps": 1,
                                   "max_history": 8, "debug": False}}
            model_src = ["17", 0]
        if use_first_block_cache:
            wf["18"] = {"class_type": "ApplyMiniMaxH3FirstBlockCache",
                        "inputs": {"model": model_src,
                                   "mode": "H3 Fast — 0.10 / max 2",
                                   "threshold": 0.10, "start_percent": 0.10,
                                   "end_percent": 0.95, "max_consecutive_hits": 2,
                                   "temporal_guard": False}}
            model_src = ["18", 0]

    wf["6"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler_name}}
    wf["7"] = {"class_type": "BasicScheduler",
               "inputs": {"model": model_src, "scheduler": scheduler,
                          "steps": steps, "denoise": 1.0}}
    wf["8"] = {"class_type": "BasicGuider",
               "inputs": {"model": model_src, "conditioning": ["5", 0]}}
    wf["9"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf["10"] = {"class_type": "SamplerCustomAdvanced",
                "inputs": {"noise": ["9", 0], "guider": ["8", 0],
                           "sampler": ["6", 0], "sigmas": ["7", 0],
                           "latent_image": ["5", 1]}}
    wf["11"] = {"class_type": "VAEDecode",
                "inputs": {"samples": ["10", 0], "vae": ["3", 0]}}
    wf["12"] = {"class_type": "VAEDecodeAudio",
                "inputs": {"samples": ["10", 0], "vae": ["4", 0]}}
    wf["13"] = {"class_type": "CreateVideo",
                "inputs": {"images": ["11", 0], "audio": ["12", 0], "fps": fps}}
    wf["14"] = {"class_type": "SaveVideo",
                "inputs": {"video": ["13", 0], "filename_prefix": "h3_shot",
                           "format": "mp4", "codec": "h264"}}
    return wf


def build_h3_retake_workflow(
    retake_prompt: str,
    base_video: str,
    base_video_frames: int,
    retake_start: int,
    retake_length: int,
    global_prompt: str = "",
    seed: int = 1,
    cfg=None,
    steps: int = 8,
    fps: int = 24,
    width: int = 854,
    height: int = 480,
    sampler_name: str = "sa_solver",
    scheduler: str = "simple",
    use_spectrum: bool = False,
    use_first_block_cache: bool = False,
    use_turbo_lora: bool = True,
    lora_strength: float = 0.75,
    retake_strength: float = 1.0,
) -> dict[str, Any]:
    """Build a Retake workflow: regenerate a marked range of an existing video.

    `base_video` is a filename in ComfyUI's input dir. When retake_start +
    retake_length reaches past the base video's end, the range is a true
    extension (head + new content, no tail). The result is spliced back in by
    MiniMaxH3RetakeStitchCS.
    """
    cfg = cfg or get_config()
    ck = cfg.get("comfy", "checkpoints", {})
    data = json.loads(build_timeline(global_prompt or retake_prompt, retake_length,
                                     segment_prompt=retake_prompt))
    data.update({
        "retakeMode": True,
        "retakeVideo": {"fileName": base_video, "imageFile": base_video,
                        "videoDurationFrames": base_video_frames},
        "retakeStart": retake_start,
        "retakeLength": retake_length,
        "retakePrompt": retake_prompt,
        "retakeStrength": retake_strength,
        "retake_global_prompt": global_prompt or retake_prompt,
    })
    timeline = json.dumps(data)

    wf: dict[str, Any] = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": ck.get("h3_fl2va"), "weight_dtype": "default"}},
        "1a": {"class_type": "LoraLoaderModelOnly",
               "inputs": {"lora_name": ck.get("h3_turbo_lora"),
                          "strength_model": lora_strength, "model": ["1", 0]}},
        "2": {"class_type": "UNETLoader",
              "inputs": {"unet_name": ck.get("h3_ref2va"), "weight_dtype": "default"}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": ck.get("h3_clip"), "type": "minimax", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": ck.get("h3_video_vae")}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": ck.get("h3_audio_vae")}},
        "6": {"class_type": "MiniMaxH3DirectorCS",
              "inputs": {
                  "model": ["1a", 0], "model_ref2va": ["2", 0],
                  "clip": ["3", 0], "vae": ["4", 0], "audio_vae": ["5", 0],
                  "global_prompt": global_prompt or retake_prompt,
                  "start_second": 0.0, "end_second": round(retake_length / fps, 3),
                  "duration_seconds": round(retake_length / fps, 3),
                  "start_frame": retake_start, "end_frame": retake_start + retake_length,
                  "duration_frames": retake_length,
                  "timeline_data": timeline,
                  "local_prompts": retake_prompt,
                  "segment_lengths": f"[{retake_length}]", "guide_strength": "",
                  "frame_rate": fps,
                  "custom_width": width, "custom_height": height,
              }},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler_name}},
        "9": {"class_type": "BasicScheduler",
              "inputs": {"model": ["6", 0], "scheduler": scheduler, "steps": steps, "denoise": 1.0}},
        "10": {"class_type": "BasicGuider",
               "inputs": {"model": ["6", 0], "conditioning": ["6", 1]}},
        "11": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0],
                          "sigmas": ["9", 0], "latent_image": ["6", 2]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["4", 0]}},
        "13": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["11", 0], "vae": ["5", 0]}},
        "16": {"class_type": "MiniMaxH3RetakeStitchCS",
               "inputs": {"retake_info": ["6", 9], "images": ["12", 0], "audio": ["13", 0]}},
        "14": {"class_type": "CreateVideo",
               "inputs": {"images": ["16", 0], "audio": ["16", 1], "fps": ["16", 2]}},
        "15": {"class_type": "SaveVideo",
               "inputs": {"video": ["14", 0], "filename_prefix": "h3_extend",
                          "format": "mp4", "codec": "h264"}},
    }
    if use_spectrum or use_first_block_cache:
        model_src: list[Any] = ["6", 0]
        if use_spectrum:
            wf["17"] = {"class_type": "SpectrumApplyMiniMaxH3",
                        "inputs": {"model": model_src, "enabled": True, "blend_weight": 0.5,
                                   "degree": 1, "ridge_lambda": 0.10, "window_size": 2.0,
                                   "flex_window": 0.75, "warmup_steps": 1, "tail_actual_steps": 1,
                                   "max_history": 8, "debug": False}}
            model_src = ["17", 0]
        if use_first_block_cache:
            wf["18"] = {"class_type": "ApplyMiniMaxH3FirstBlockCache",
                        "inputs": {"model": model_src,
                                   "mode": "H3 Fast — 0.10 / max 2",
                                   "threshold": 0.10, "start_percent": 0.10,
                                   "end_percent": 0.95, "max_consecutive_hits": 2,
                                   "temporal_guard": False}}
            model_src = ["18", 0]
        wf["9"]["inputs"]["model"] = model_src
        wf["10"]["inputs"]["model"] = model_src
    return wf


def run_h3_shot(client: ComfyClient, workflow: dict[str, Any], out_path: Path | str,
                timeout_s: float = 1800.0) -> Path:
    prompt_id = client.submit(workflow)
    entry = client.wait(prompt_id, timeout_s=timeout_s)
    if entry.get("status", {}).get("status_str") == "error":
        msgs = entry.get("status", {}).get("messages", [])
        err = [m[1] for m in msgs if m[0] == "execution_error"]
        raise RuntimeError(f"render failed: {err[0] if err else 'see ComfyUI console'}")
    videos = []
    for node_id, output in (entry.get("outputs") or {}).items():
        if "video" in output:
            videos.extend(output["video"] if isinstance(output["video"], list) else [output["video"]])
        if "gifs" in output:
            videos.extend(output["gifs"])
        if output.get("images") and output.get("animated"):
            videos.extend(output["images"])
    if not videos:
        raise RuntimeError(f"no video output for prompt {prompt_id}")
    v = videos[0]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    client.download(v["filename"], subfolder=v.get("subfolder", ""),
                    node_type=v.get("type", "output"), dest=str(out))
    return out
