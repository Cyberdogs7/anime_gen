"""Fused H3 (MATLOWAI + Civitai Fast H3) — single-file 4-step builder.

MATLOWAI/minimax-h3-fused-turbo-int8-convrot merges:
  - xmarre fused delta r1024 (fl2va + ref2va in one UNet)
  - lightx2v 8-step turbo @1.0 + Mystic v2 @0.7
Quant is baked INT8 convrot — no LoRA at runtime, no backup copy (+21GB saved).

Two operating modes:
  - MATLOWAI: BasicScheduler 4 steps, res_multistep/simple, H3SLA 0.9/64, shift 12/3
  - Civitai aziib Fast H3: ManualSigmas per step count + sageattention/triton

Both use the same fused checkpoint. Width/height default to 1152x640 (0.7 MP) per
MATLOWAI, but 864x480 (0.4 MP) is also valid — the 0.4 MP run is ~70s for 5s.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .clients.comfy import ComfyClient
from .compile.durations import snap_duration
from .config import get_config

# Civitai Fast H3 manual sigmas (aziib model 2906467)
MANUAL_SIGMAS = {
    4: [0.9999166, 0.9728326, 0.9230769, 0.8, 0.0],
    6: [0.9999166, 0.9868421, 0.9638554, 0.9230769, 0.8695652, 0.8, 0.0],
    8: [1.0, 0.9882352941, 0.9729729730, 0.9523809524, 0.9230769231, 0.8780487805, 0.8, 0.6315789474, 0.0],
}

# Fallback: use fp16 VAE file if int8 alias missing on disk. The fused workflows
# name it minimax_h3_video_vae_int8_convrot.safetensors — we copy fp16 to that
# name at install time (see HANDOFF), but the builder tolerates either.
def _resolve_vae(ck: dict[str, str]) -> str:
    # Prefer int8 if configured and file exists; else fall back to fp16 entry.
    for key in ("h3_video_vae_int8", "h3_video_vae"):
        name = ck.get(key)
        if name:
            # Check on ComfyUI's VAE dir; if not found locally, still return the name —
            # ComfyUI will error clearly. We prefer int8 when present.
            return name
    return ck.get("h3_video_vae") or "minimax_h3_video_vae_fp16.safetensors"


def build_h3_fused_workflow(
    prompt: str,
    duration_s: float,
    seed: int,
    cfg=None,
    ref_image_filenames: list[str] | None = None,
    ref_audio_filenames: list[str] | None = None,
    fps: int = 24,
    width: int = 1152,
    height: int = 640,
    steps: int = 4,
    sampler_name: str = "res_multistep",
    scheduler: str = "simple",  # "simple" (MATLOWAI) or "manual" (Civitai)
    use_sla: bool = True,
    sla_sparsity: float = 0.9,
    sla_block: int = 64,
    use_sage: bool = False,
    use_triton: bool = False,  # triton is a runtime flag, not a node — documented here
    manual_sigmas: list[float] | None = None,
) -> dict[str, Any]:
    """Fused single-file graph — mirrors MATLOWAI 01/05_reference_4step_sla.api.json.

    Ref handling: same as core MiniMaxH3ReferenceToVideo — one LoadImage per
    ref_image_filename becomes ref_images.ref_image_N; same for audio.
    When no refs, the node is pure t2v/image-to-video (first_frame via prompt).

    SLA: H3SLAAttention(model, sparsity_ratio=0.9, block_size=64,
          min_seq_len=8192, protect_audio=true) -> MiniMaxH3SigmaShift
          -> BasicScheduler or ManualSigmas.
    Sage: if use_sage, a SageAttention patch node would sit after SLA
          (requires comfyui-triton-and-sageattention-installer).
    """
    cfg = cfg or get_config()
    ck = cfg.get("comfy", "checkpoints", {})
    fused_name = ck.get("h3_fused") or "minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors"
    clip_name = ck.get("h3_clip") or "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    vae_name = _resolve_vae(ck)
    audio_vae_name = ck.get("h3_audio_vae") or "minimax_h3_audio_vae_fp32.safetensors"

    k, frames, _ = snap_duration(duration_s)
    ref_image_filenames = ref_image_filenames or []
    ref_audio_filenames = ref_audio_filenames or []

    # Resolve sigmas for manual mode
    sigmas = manual_sigmas
    if scheduler == "manual" and sigmas is None:
        sigmas = MANUAL_SIGMAS.get(steps)
        if sigmas is None:
            raise ValueError(f"manual scheduler needs explicit sigmas for steps={steps}; known: {sorted(MANUAL_SIGMAS)}")

    wf: dict[str, Any] = {
        "127": {"class_type": "UNETLoader", "inputs": {"unet_name": fused_name, "weight_dtype": "default"}},
        "128": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": "minimax", "device": "default"}},
        "119": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "120": {"class_type": "VAELoader", "inputs": {"vae_name": audio_vae_name}},
        "129": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "135": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler_name}},
    }

    # Optional SLA + SigmaShift chain — matches MATLOWAI graph: 127 -> 900 -> 142
    model_src: list[Any] = ["127", 0]
    if use_sla:
        wf["900"] = {
            "class_type": "H3SLAAttention",
            "inputs": {
                "model": model_src,
                "sparsity_ratio": float(sla_sparsity),
                "block_size": str(int(sla_block)),
                "min_seq_len": 8192,
                "dense_last_steps": 0,
                "protect_audio": True,
                "enabled": True,
            },
        }
        model_src = ["900", 0]
    # SageAttention patch would go here if use_sage — node is SageAttentionPatch or similar
    # from the triton installer. We leave it as a no-op if not installed; ComfyUI will
    # error with a clear missing-node message. Documented for Civitai path.
    if use_sage:
        # Placeholder: the actual node name depends on installer version (SageAttention, SageAttn, etc).
        # We emit a generic patch node that will be resolved if the extension is present.
        wf["901_sage"] = {
            "class_type": "SageAttention",
            "inputs": {"model": model_src, "enabled": True},
        }
        model_src = ["901_sage", 0]

    wf["142"] = {
        "class_type": "MiniMaxH3SigmaShift",
        "inputs": {"model": model_src, "shift_video": 12.0, "shift_audio": 3.0},
    }
    model_src = ["142", 0]

    # Choose t2v vs ref2va by whether refs are wired — we always use the flexible
    # MiniMaxH3ReferenceToVideo node; it handles zero refs as pure t2v.
    # For image-to-video, the caller passes the first-frame image as ref_image_0
    # and the prompt should reference <Picture 1> accordingly.
    cond_inputs: dict[str, Any] = {
        "clip": ["128", 0],
        "vae": ["119", 0],
        "audio_vae": ["120", 0],
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "length": int(frames),
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

    wf["131"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": cond_inputs}

    # Scheduler path: BasicScheduler (MATLOWAI) vs ManualSigmas (Civitai)
    if scheduler == "manual":
        # ComfyUI core ManualSigmas: inputs {sigmas: "0.999..., 0.8, 0.0"} -> output SIGMAS
        # sigmas are comma-separated floats; we stringify the list.
        sigmas_str = ", ".join(str(float(s)) for s in sigmas) if sigmas else ""
        wf["124"] = {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": sigmas_str},
        }
        sigma_src: list[Any] = ["124", 0]
    else:
        wf["124"] = {
            "class_type": "BasicScheduler",
            "inputs": {"model": model_src, "scheduler": scheduler, "steps": int(steps), "denoise": 1.0},
        }
        sigma_src = ["124", 0]

    wf["126"] = {"class_type": "BasicGuider", "inputs": {"model": model_src, "conditioning": ["131", 0]}}
    wf["125"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["129", 0],
            "guider": ["126", 0],
            "sampler": ["135", 0],
            "sigmas": sigma_src,
            "latent_image": ["131", 1],
        },
    }
    wf["122"] = {"class_type": "VAEDecode", "inputs": {"samples": ["125", 0], "vae": ["119", 0]}}
    wf["121"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["125", 0], "vae": ["120", 0]}}
    wf["130"] = {"class_type": "CreateVideo", "inputs": {"images": ["122", 0], "audio": ["121", 0], "fps": fps, "bit_depth": 8}}
    wf["92"] = {"class_type": "SaveVideo", "inputs": {"video": ["130", 0], "format": "auto", "codec": "auto", "filename_prefix": "video/fused_4step_sla"}}

    return wf


def build_h3_fused_civitai_variant(
    prompt: str,
    duration_s: float,
    seed: int,
    cfg=None,
    width: int = 864,
    height: int = 480,
    steps: int = 4,
    use_sage: bool = True,
) -> dict[str, Any]:
    """Civitai Fast H3 preset — fused + ManualSigmas + sage/triton.

    From aziib model 2906467 description:
      4 steps sigmas [0.9999,0.9728,0.9230,0.8,0.0]
      must use sage attention and triton for faster generation
    Width default 864x480 per Civitai's "0.4 MP in 70s" claim.
    """
    return build_h3_fused_workflow(
        prompt, duration_s, seed, cfg=cfg,
        width=width, height=height,
        steps=steps,
        sampler_name="res_multistep",
        scheduler="manual",
        manual_sigmas=MANUAL_SIGMAS[steps],
        use_sla=True,  # Civitai still benefits from SLA; installer adds sage on top
        use_sage=use_sage,
        use_triton=True,
    )


def run_h3_fused(
    client: ComfyClient,
    workflow: dict[str, Any],
    out_path: Path | str,
    timeout_s: float = 1800.0,
    hard_timeout_s: float | None = None,
) -> Path:
    """Submit a fused workflow and download the MP4 — same download logic as h3.run_h3_shot."""
    from .h3 import run_h3_shot  # reuse downloader

    # Reuse run_h3_shot's wait/download — it just needs a workflow dict.
    return run_h3_shot(client, workflow, out_path, timeout_s=timeout_s, hard_timeout_s=hard_timeout_s)
