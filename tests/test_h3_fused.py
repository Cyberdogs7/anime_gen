"""Fused H3 (MATLOWAI + Civitai) — workflow structure tests.

These are offline (no GPU) — they just build the ComfyUI graphs and assert
the correct nodes/sockets/steps are wired. The live render tests are skipped
unless ComfyUI is reachable at 127.0.0.1:8188.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.h3 import build_h3_ref2va_workflow
from studio.h3_fused import (
    MANUAL_SIGMAS,
    build_h3_fused_civitai_variant,
    build_h3_fused_workflow,
)


def test_fused_4step_sla_structure():
    wf = build_h3_fused_workflow(
        "a cinematic test prompt", 5.17, 42,
        width=1152, height=640, steps=4,
        sampler_name="res_multistep", scheduler="simple",
        use_sla=True, use_sage=False,
    )
    # Fused checkpoint — single file, no LoRA
    assert wf["127"]["inputs"]["unet_name"].startswith("minimax_h3_fused")
    assert "1a" not in wf  # no LoRA
    # SLA chain
    assert wf["900"]["class_type"] == "H3SLAAttention"
    assert wf["900"]["inputs"]["sparsity_ratio"] == 0.9
    assert wf["900"]["inputs"]["block_size"] == "64"
    assert wf["142"]["class_type"] == "MiniMaxH3SigmaShift"
    # Scheduler 4 steps
    assert wf["124"]["inputs"]["steps"] == 4
    assert wf["124"]["inputs"]["scheduler"] == "simple"
    # Sampler
    assert wf["135"]["inputs"]["sampler_name"] == "res_multistep"
    # Core node
    assert wf["131"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert wf["131"]["inputs"]["width"] == 1152


def test_fused_manual_sigmas_civitai_4step():
    wf = build_h3_fused_civitai_variant(
        "prompt", 5.17, 1, width=864, height=480, steps=4, use_sage=False
    )
    # Manual scheduler path — sigmas are stored as comma string for ManualSigmas node
    expected_str = ", ".join(str(float(s)) for s in MANUAL_SIGMAS[4])
    assert wf["124"]["inputs"]["sigmas"] == expected_str
    assert len(MANUAL_SIGMAS[4]) == 5  # N+1
    assert wf["124"]["class_type"] == "ManualSigmas"
    # Civitai default 0.4 MP
    assert wf["131"]["inputs"]["width"] == 864


def test_fused_manual_sigmas_8step():
    wf = build_h3_fused_workflow(
        "p", 5.17, 1, steps=8, scheduler="manual", manual_sigmas=MANUAL_SIGMAS[8]
    )
    expected_str = ", ".join(str(float(s)) for s in MANUAL_SIGMAS[8])
    assert wf["124"]["inputs"]["sigmas"] == expected_str
    assert len(MANUAL_SIGMAS[8]) == 9


def test_fused_ref_sockets():
    wf = build_h3_fused_workflow(
        "ref test", 5.17, 1,
        ref_image_filenames=["ref1.png", "ref2.png"],
        ref_audio_filenames=["voice.wav"],
    )
    cond = wf["131"]["inputs"]
    assert cond["ref_images.ref_image_0"] == ["img0", 0]
    assert cond["ref_images.ref_image_1"] == ["img1", 0]
    assert cond["ref_audios.ref_audio_0"] == ["aud0", 0]
    assert wf["img0"]["class_type"] == "LoadImage"
    assert wf["aud0"]["class_type"] == "LoadAudio"


def test_baseline_still_has_lora():
    # Baseline ref2va must still wire the turbo LoRA — ensures we didn't break old path.
    wf = build_h3_ref2va_workflow("baseline", 5.17, 1)
    assert "1a" in wf  # LoraLoaderModelOnly for ref2v turbo
    assert wf["1a"]["class_type"] == "LoraLoaderModelOnly"


def test_manual_sigmas_dict_completeness():
    assert set(MANUAL_SIGMAS.keys()) == {4, 6, 8}
    for k, v in MANUAL_SIGMAS.items():
        assert v[0] > 0.99
        assert v[-1] == 0.0
        assert v == sorted(v, reverse=True)
