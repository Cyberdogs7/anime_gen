"""Episode video rendering: storyboard keyframes -> H3 video shots (DESIGN §10.3).

Turns an approved episode script + its storyboard keyframes into video. Each shot
is one MiniMax H3 generation:

  1. compile the shot's MiniMax-notation prompt (deterministic, no LLM),
  2. upload the character refs + the shot's storyboard keyframe as ref2va inputs,
  3. build the H3 Director workflow, run it, save the MP4 to runs/EP##/video/.

Runs in a background thread with progress (mirrors the storyboard job model).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import fsutil
from .bootstrap import ACTIVITY
from .clients.comfy import ComfyClient
from .config import get_config
from .show import Show

# Guide path imports are optional — the pack is a ComfyUI custom_node, not a pip dep.
# We import lazily inside compile_shot_prompt_guide to avoid hard torch dependency;
# dummy media objects below provide the required .shape for validation.
try:
    import sys as _sys
    _sys.path.insert(0, r"D:\anime-h3\ComfyUI\custom_nodes\ComfyUI-MiniMax-H3-Guide")
    from plan_v2 import (  # type: ignore
        MiniMaxH3PlanV2ProjectSetup,
        MiniMaxH3PlanV2ImageReference,
        MiniMaxH3PlanV2AudioReference,
        MiniMaxH3PlanV2Shot,
        MiniMaxH3PlanV2DialogueEvent,
        IMAGE_DEFINE_VISIBLE,
        CONTENT_IDENTITY,
        RETENTION_AUTO,
        AUDIO_VOICE,
    )
    _GUIDE_AVAILABLE = True
except Exception:
    _GUIDE_AVAILABLE = False

# Background jobs: show_id -> {"state", "done", "total", "detail"}
RENDER_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _latest_script(show: Show, episode: int) -> dict[str, Any] | None:
    d = show.dir / "runs" / f"EP{episode:02d}"
    scripts = fsutil.existing_by_mtime(d.glob("script.r*.json"))
    if not scripts:
        return None
    return json.loads(scripts[-1].read_text(encoding="utf-8"))


def _prompts_path(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "prompts.json"


def _load_prepared_prompts(show: Show, episode: int) -> dict[str, Any]:
    """Read the persisted per-shot prompts for an episode (Pass 1 output)."""
    p = _prompts_path(show, episode)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def prepare_episode_prompts(show: Show, episode: int, cfg=None,
                            progress=None) -> int:
    """Pass 1 (LLM-resident): compile + persist the final H3 prompt for every shot.

    Runs while the LLM owns the GPU (its only render-path consumer), so H3 never
    has to fight the LLM for VRAM. When ``h3_rewrite_prompt`` is enabled, the
    deterministic short prompt is expanded once per shot into a rich production
    brief; the result is written to runs/EP##/prompts.json so the render pass
    (Pass 2) does zero LLM work. Returns the number of shots prepared.
    """
    from .gpu_manager import ServiceType, get_gpu_manager
    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    if not script:
        return 0
    rewrite = bool(cfg.get("pipeline", "h3_rewrite_prompt", False))

    shots = [(sc, sh) for sc in script.get("scenes", [])
             for sh in sc.get("shots", [])]
    prompts: dict[str, Any] = {}

    def _one(sc: dict[str, Any], sh: dict[str, Any]) -> None:
        from .storyboard import _shot_refs
        sid = sh.get("id", "shot")
        names, _ = _shot_refs(show, sh)
        # Guide path: beat-based, shadow-sanitized, minimal silence — fallback to legacy on error
        try:
            compiler = cfg.get("pipeline", "prompt_compiler", "guide")
        except Exception:
            compiler = "guide"
        if compiler == "guide" and _GUIDE_AVAILABLE:
            base = compile_shot_prompt_guide(show, script, sc, sh, names)
        else:
            base = compile_shot_prompt(show, script, sc, sh, names)
        prompts[sid] = {"base": base, "rewritten": base, "rewrite": rewrite}
        if rewrite:
            from .clients.lmstudio import LMStudioClient
            from .prompts import h3_rewrite_prompt
            llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=600)
            try:
                prompts[sid]["rewritten"] = h3_rewrite_prompt(llm, base, sh)
            except Exception as exc:
                log.warning("H3 prompt rewriter failed for %s; "
                            "using deterministic prompt: %s", sid, exc)

    if rewrite:
        with get_gpu_manager(cfg).acquire(ServiceType.LLM):
            for i, (sc, sh) in enumerate(shots):
                _one(sc, sh)
                if progress:
                    progress(i + 1, len(shots), sh.get("id", "shot"))
            p = _prompts_path(show, episode)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(prompts), encoding="utf-8")
            return len(shots)

    # No LLM pass: just compile deterministically (no GPU needed).
    for i, (sc, sh) in enumerate(shots):
        _one(sc, sh)
        if progress:
            progress(i + 1, len(shots), sh.get("id", "shot"))
    p = _prompts_path(show, episode)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prompts), encoding="utf-8")
    return len(shots)


def _render_client(cfg=None) -> ComfyClient:
    cfg = cfg or get_config()
    node = cfg.get("comfy", "nodes", {}).get("renderer", {})
    return ComfyClient(node.get("url", "http://127.0.0.1:8188"),
                       node.get("api_key"))


def _shot_ref_paths(show: Show, episode: int, scene: dict[str, Any],
                    shot: dict[str, Any]) -> list[str]:
    """Reference-image paths for a shot: object refs + the shot's storyboard
    keyframe (composition anchor). Character refs are NOT included here — they
    travel in the timeline's ``characters`` slots so the Director binds each to
    a <Subject N> definition."""
    from .storyboard import _shot_object_refs
    paths: list[str] = []
    for obj in _shot_object_refs(show, episode, shot):
        if obj not in paths:
            paths.append(obj)
    sid = shot.get("id", "")
    kf = show.dir / "runs" / f"EP{episode:02d}" / "storyboard" / f"{sid}.png"
    if kf.exists():
        paths.append(str(kf))
    return paths


def _shot_character_ref(show: Show, name: str) -> str | None:
    """First approved reference-image path for one character, or None."""
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") != name:
            continue
        rd = show.character_refs_dir(cid)
        rj = rd / "refs.json"
        if not rj.exists():
            continue
        try:
            prior = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
        if prior.get("status") != "real":
            continue
        refs = prior.get("refs") or []
        if refs and (rd / refs[0]).exists():
            return str(rd / refs[0])
    return None


def _voice_sample_path(show: Show, name: str) -> Path | None:
    """Approved voice-sample path for a character name (assets/voice/<id>_voice.wav)."""
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") == name:
            vp = show.dir / "assets" / "voice" / f"{cid}_voice.wav"
            return vp if vp.exists() else None
    return None


def _character_appearance(show: Show, name: str) -> str:
    """appearance_canonical for a character name, or ''."""
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") == name:
            return (c.get("appearance_canonical") or "").strip()
    return ""


def _has_speech(line: str) -> bool:
    """True when a dialogue entry actually speaks words.

    A parenthetical stage direction like ``(Grunting)`` or ``(Internal)`` carries
    no spoken text and must never become an H3 ``<d>`` token — H3 would try to
    synthesize it as gibberish speech. A leading note like ``(whispering)`` is fine
    as long as real words follow.
    """
    body = re.sub(r"\([^)]*\)", "", line or "")
    return bool(re.search(r"\w", body))


def _spoken_entries(shot: dict[str, Any]) -> list[dict[str, Any]]:
    """Dialogue entries that are REAL spoken lines (not stage directions)."""
    return [d for d in (shot.get("dialogue") or [])
            if _has_speech(d.get("line") or "")]


def _line_frag(d: dict[str, Any], line: str, subj: str, sid: str,
               audio: str) -> str:
    """Render one spoken line as an H3 speech fragment.

    Per MiniMax's Video Prompt Writing Guide §4.4 and the Ref2VA guide §5.4,
    the speaker's identifying phrase, action, delivery and speaker ID go OUTSIDE
    the ``<d>`` token; only the language tag and exact words go inside. An
    on-camera speaker gets lip-sync; an off-screen/voiceover line explicitly
    states the character's lips remain completely closed so H3 never fabricates
    mouth motion or background chatter.
    """
    delivery = (d.get("delivery") or "").strip()
    on_cam = bool(d.get("on_camera", True))
    speaker = f"{subj} {sid}" if subj else "A voice"

    if on_cam:
        head = f"{speaker} says"
        if delivery:
            head += f" {delivery}"
        if audio:
            head += f", using the voice timbre referenced from {audio}"
        return f"{head}, <d>[English] {line}</d>"

    head = f"{speaker} says in an off-screen voiceover"
    if delivery:
        head += f" {delivery}"
    if audio:
        head += f", using the voice timbre referenced from {audio}"
    return f"{head}, <d>[English] {line}</d> while their lips remain completely closed."


def compile_shot_prompt(show: Show | None, script: dict[str, Any],
                        scene: dict[str, Any], shot: dict[str, Any],
                        names: list[str],
                        audio_refs: list[dict[str, str]] | None = None) -> str:
    """Full-Reference Mode rewrite prompt, EXACTLY per MiniMax's
    VIDEO_PROMPT_WRITING_GUIDE_ref_en.md (six sections, in order):

        subject_definitions
        summary
        retention_analysis
        detailed_description
        overall_soundscape
        non_diegetic_music

    Rules followed verbatim from the guide:
    - ``<Subject N>`` is the reusable person/identity; ``<Picture N>`` is the
      raw reference frame it comes from. Define each subject as
      ``<Subject N> is <name>, shown in <Picture N>, <appearance>.``
    - ``<Audio N>`` bound to its speaker:
      ``<Audio 1> is the voice-timbre reference for <Subject 1> (S1).``
    - ``summary`` begins with a task-type prefix: ``[reference generation +
      audio reference]``.
    - ``retention_analysis``: ``<Subject N> (appears in [Shot 1]):
      fully_preserved - ...`` and ``<Audio N>: reference - its vocal timbre
      guides the dialogue delivery of <Subject N> without copying the original
      signal.`` (no (Sx) here).
    - ``detailed_description``: style opening, then ``[Shot 1] ...`` with
      speakers as ``<Subject N> (Sx)`` and dialogue ONLY inside
      ``<d>[English] exact words.</d>``. Delivery/off-screen/voiceover notes go
      OUTSIDE the token; off-screen lines state the lips remain closed. When the
      shot has no spoken lines, an explicit silence clause is emitted so H3 never
      invents (gibberish) speech. (See PROMPTING.md §5.4.)
    - ``overall_soundscape`` / ``non_diegetic_music`` last; ``N/A`` for the
      soundscape means complete silence and is only emitted for silent shots.

    ``audio_refs`` carries {"char": name, "token": "<Audio N>"} per voice ref.
    """
    # --- subject_definitions ---
    subject_by_name: dict[str, str] = {}
    subject_lines: list[str] = []
    for i, name in enumerate(names):
        pic = f"<Picture {i + 1}>"
        subj = f"<Subject {i + 1}>"
        subject_by_name[name] = subj
        appearance = _character_appearance(show, name) if show else ""
        appearance = appearance[:1].lower() + appearance[1:] if appearance else ""
        appearance = appearance.rstrip(" .") if appearance else ""
        subject_lines.append(f"{subj} is {name}, shown in {pic}"
                             + (f", {appearance}" if appearance else "") + ".")
    for a in (audio_refs or []):
        char = a.get("char", "")
        subj = subject_by_name.get(char, f"<Subject {len(subject_by_name) + 1}>")
        sid = _speaker_id(char, shot)
        subject_lines.append(f"{a['token']} is the voice-timbre reference "
                             f"for {subj} {sid}.")

    # --- summary ---
    # Use shot/scene-local context, not episode-level script.summary (which bleeds 4+ irrelevant names into a 2-person bar shot and truncates to "…a").
    # Prefer scene summary / shot beat which actually describe this shot's location and action.
    raw_global = (
        (shot.get("beat") or "").strip()
        or (scene.get("summary") or "").strip()
        or (script.get("summary") or "").strip()
    )
    # Word-boundary truncate to avoid dangling " a" / mid-word cut that confuses the tokenizer
    if len(raw_global) > 300:
        cut = raw_global[:300].rsplit(" ", 1)[0]
        global_desc = cut.strip()
    else:
        global_desc = raw_global.strip()
    task = "[reference generation" + (" + audio reference" if audio_refs else "") + "]"
    summary_txt = f"{task} {global_desc or 'One cinematic shot.'}"

    # --- retention_analysis ---
    retention_lines: list[str] = []
    for name in names:
        subj = subject_by_name.get(name, f"<Subject {len(subject_by_name) + 1}>")
        retention_lines.append(
            f"{subj} (appears in [Shot 1]): fully_preserved - {name}'s identity, "
            f"clothing and appearance are retained exactly as shown.")
    for a in (audio_refs or []):
        char = a.get("char", "")
        subj = subject_by_name.get(char, "<Subject 1>")
        retention_lines.append(
            f"{a['token']}: reference - its vocal timbre guides the dialogue "
            f"delivery of {subj} without copying the original signal.")

    # --- detailed_description ---
    placed: list[str] = []
    for name in names:
        subj = subject_by_name.get(name, "")
        appearance = _character_appearance(show, name) if show else ""
        appearance = appearance[:1].lower() + appearance[1:] if appearance else ""
        appearance = appearance.rstrip(" .") if appearance else ""
        if appearance:
            placed.append(f"{subj} ({name}), {appearance}")
        else:
            placed.append(f"{subj} ({name})")
    placed_txt = ", ".join(placed)

    parts: list[str] = []
    action = (shot.get("action") or "").strip()
    cam = (shot.get("camera") or "").strip()
    if action:
        parts.append(action)
    if cam:
        parts.append(cam)
    spoken = _spoken_entries(shot)
    for d in spoken:
        line = (d.get("line") or "").strip()
        char = d.get("char", "")
        subj = subject_by_name.get(char, "")
        sid = _speaker_id(char, shot)
        audio = next((a["token"] for a in (audio_refs or [])
                      if a.get("char") == char), "")
        parts.append(_line_frag(d, line, subj, sid, audio))
    # Dialogue/silence discipline: H3 renders video AND audio jointly, and it
    # treats bare prose as narration — an un-prompted, unspecified audio track
    # degenerates into gibberish speech. Every shot must state its speech state
    # EXACTLY: either every spoken line as a <d> token above, or an explicit
    # silence clause when nobody speaks. The `silence` flag (from the dialogue
    # pass / _normalize) makes the decision explicit in data; the clause below
    # makes it explicit in the prompt H3 actually sees.
    if not spoken:
        parts.append("No one speaks; all characters remain silent. "
                     "The shot carries no dialogue.")
    shot_txt = " ".join(p for p in parts if p)

    style = (script.get("style_guide") or
             "The target video uses a cinematic 2D-anime style.").strip()
    detailed = f"{style}\n[Shot 1] {placed_txt}. {shot_txt}".strip()

    # --- overall_soundscape / non_diegetic_music ---
    # overall_soundscape holds ONLY ambience and physical action sounds; speech
    # lives in detailed_description. `N/A` means complete silence, so it may only
    # be emitted for a shot that is truly silent — never for a shot with dialogue.
    soundscape = (shot.get("soundscape") or "").strip()
    if not soundscape:
        soundscape = ("No background ambience; the only voices are the spoken "
                      "lines in detailed_description." if spoken
                      else "Complete silence around the action; "
                           "no speech and no ambience.")
    music = (shot.get("music") or "").strip()

    sections = [
        "subject_definitions:\n" + "\n".join(subject_lines),
        f"summary:\n{summary_txt}",
        "retention_analysis:\n" + "\n".join(retention_lines),
        f"detailed_description:\n{detailed}",
        f"overall_soundscape:\n{soundscape or 'N/A'}",
        f"non_diegetic_music:\n{music or 'N/A'}",
    ]
    return "\n\n".join(sections).strip()


def _guide_dummy_image():
    """Dummy IMAGE tensor for Guide validation without requiring torch."""
    class _Dummy:
        shape = (1, 480, 864, 3)
    return _Dummy()

def _guide_dummy_audio(duration_s: float = 3.0, sample_rate: int = 48000):
    """Dummy AUDIO dict for Guide voice-timbre validation (no torch)."""
    samples = int(sample_rate * max(2.0, min(15.0, duration_s)))
    class _Wav:
        shape = (1, 1, samples)
    return {"waveform": _Wav(), "sample_rate": sample_rate}

def _sanitize_initial_prompt(raw: str) -> str:
    """Beat-based sanitization: neutralize shadow/silhouette phrasing and word-truncate.

    Show-agnostic: no character names or show-specific strings are special-cased
    here. "X shadow looming over Y" makes the model render a spurious second
    silhouette, so it is rewritten to "X standing nearby" (dropping the trailing
    "over <target>") before the prompt is compiled.
    """
    if not raw:
        return ""
    raw = re.sub(r"\bshadow\s+loom(?:ing|s)\b(?:\s+over\s+[^;.]+)?",
                 "standing nearby", raw)
    raw = raw.strip()
    if len(raw) > 300:
        raw = raw[:300].rsplit(" ", 1)[0].strip()
    return raw

def compile_shot_prompt_guide(show: Show | None, script: dict[str, Any],
                              scene: dict[str, Any], shot: dict[str, Any],
                              names: list[str]) -> str:
    """Guide path: build a Plan v2 for ONE shot and compile via plan_v2.compile_h3_plan.

    Uses beat-based initial_prompt (not episode labyrinth), minimal silence,
    no shadow duplication, and dummy media for validation. The returned prompt
    is compatible with the same fused ReferenceToVideo workflow (same seed
    determinism as legacy). Falls back to legacy compile on any failure.
    """
    if not _GUIDE_AVAILABLE:
        return compile_shot_prompt(show, script, scene, shot, names)
    try:
        from plan_v2 import compile_h3_plan  # local import to avoid top-level hard dep
        from studio.compile.durations import snap_duration
        # initial_prompt: shot beat > scene summary (beat-based, shadow-sanitized)
        raw_initial = (shot.get("beat") or scene.get("summary") or "").strip()
        if not raw_initial:
            raw_initial = (script.get("summary") or "One cinematic shot.").strip()
        initial_prompt = _sanitize_initial_prompt(raw_initial)
        visual_style = (script.get("style_guide") or "cinematic 2D-anime style").strip()
        soundscape = (shot.get("soundscape") or "").strip()
        music = (shot.get("music") or "N/A").strip()
        # snap duration for native grid validation
        _, _, snap_s = snap_duration(float(shot.get("duration_s", 7.29)))
        # Build plan
        setup = MiniMaxH3PlanV2ProjectSetup()
        plan, _, _ = setup.start(initial_prompt, snap_s, visual_style, soundscape, music)
        # Add image refs (dummy tensors, real files are uploaded separately in _render_shot)
        for name in names:
            c_text = ""
            if show:
                for cid in show.list_characters():
                    cc = show.read_character(cid)
                    if cc.get("name") == name:
                        c_text = (cc.get("appearance_canonical") or "")[:500]
                        break
            dummy = _guide_dummy_image()
            plan, _, _, _ = MiniMaxH3PlanV2ImageReference().add_image(
                plan, dummy, IMAGE_DEFINE_VISIBLE, f"{name} identity",
                c_text or name, CONTENT_IDENTITY, name, RETENTION_AUTO, "", ""
            )
        # Add voice-timbre audio refs + dialogue events for spoken entries
        spoken = _spoken_entries(shot)
        # Audio refs must be added in setup phase, before shots
        # We need to add them before shot creation, so recreate plan with audio first?
        # Our plan currently has images and no shots; audio can be added now before shot.
        # For simplicity, if spoken, add dummy audio for each distinct speaker who is in names
        seen = []
        for d in spoken:
            char = d.get("char")
            if char and char not in seen and char in names and len([a for a in plan["assets"] if a["media_kind"]=="audio"]) < 3:
                seen.append(char)
                dummy_audio = _guide_dummy_audio(3.0)
                plan, _, _ = MiniMaxH3PlanV2AudioReference().add_audio(
                    plan, dummy_audio, AUDIO_VOICE, f"{char} voice", char, "", "", "", "", ""
                )
        # Shot description: subjects + action (or silence) + camera (camera is separate field)
        action_txt = (shot.get("action") or "").strip()
        # Prefer beat sanitized if action empty (per-shot action is often empty, beat holds the description)
        if not action_txt:
            action_txt = _sanitize_initial_prompt((shot.get("beat") or "").strip())
        camera_txt = (shot.get("camera") or "").strip()
        if names:
            subj_clause = ", ".join([f"<Subject {i+1}> ({n})" for i, n in enumerate(names)])
            if spoken:
                # Dialogue shot: subjects + action, dialogue handled via events
                guide_desc = f"{subj_clause}. {action_txt}".strip() if action_txt else f"{subj_clause}."
            else:
                # Silent: subjects + minimal silence (no extra negatives)
                minimal_clause = "No one speaks; all characters remain silent. The shot carries no dialogue."
                guide_desc = f"{subj_clause}. {minimal_clause}"
        else:
            if spoken and action_txt:
                guide_desc = action_txt
            elif spoken:
                guide_desc = "The scene plays out as described."
            else:
                guide_desc = "No one speaks; all characters remain silent. The shot carries no dialogue."
        # Add shot
        shot_node = MiniMaxH3PlanV2Shot()
        plan, _, _ = shot_node.add_shot(plan, 0.0, guide_desc, camera_txt, "Direct cut")
        # Dialogue events (after shot)
        for d in spoken:
            char = d.get("char", "").strip()
            line = d.get("line", "").strip()
            if not char or not line:
                continue
            delivery = (d.get("delivery") or "").strip()
            on_cam = bool(d.get("on_camera", True))
            voice_mode = "On-screen speech" if on_cam else "Off-screen speech"
            plan, _ = MiniMaxH3PlanV2DialogueEvent().add_dialogue(
                plan, char, "English", line, delivery, voice_mode, "Complete in this Shot", -1
            )
        guide_prompt, _, report, compiled, _ = compile_h3_plan(plan)
        return guide_prompt
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Guide compile failed for %s, falling back to legacy: %s", shot.get("id"), exc)
        return compile_shot_prompt(show, script, scene, shot, names)


def _speaker_id(char: str, shot: dict[str, Any]) -> str:
    """(Sx) per the shot's speech order — first spoken line -> S1, etc."""
    seen = [d.get("char") for d in _spoken_entries(shot)]
    idx = seen.index(char) + 1 if char in seen else 1
    return f"(S{idx})"


def _render_shot(show: Show, episode: int, client: ComfyClient, cfg,
                 script: dict[str, Any], scene: dict[str, Any],
                 shot: dict[str, Any], seed: int,
                 timeout_s: float = 1800.0) -> Path:
    from .h3 import build_h3_ref2va_workflow, run_h3_shot
    from .storyboard import _shot_refs
    sid = shot.get("id", "shot")
    names, _ = _shot_refs(show, shot)

    # Official ref2va graph: refs are SOCKETS on MiniMaxH3ReferenceToVideo.
    # Images (character refs first, then objects/keyframe) become <Picture N>;
    # voice samples become <Audio N>. The prompt references those tags.
    # Character refs are the identity anchors — they must come first so their
    # <Picture N> numbers match `names`.
    image_filenames: list[str] = []
    for name in names:
        ref = _shot_character_ref(show, name)
        if not ref:
            continue
        try:
            image_filenames.append(client.upload_image(ref))
        except Exception:
            continue
    for p in _shot_ref_paths(show, episode, scene, shot):
        try:
            fname = client.upload_image(p)
            if fname not in image_filenames:
                image_filenames.append(fname)
        except Exception:
            pass

    # Native dialogue: each REAL spoken line's character gets a voice sample as
    # an <Audio N> reference (the PROVEN example workflow uploads the raw wav
    # as-is — no conversion). Only actual speech triggers an audio ref: a silent
    # shot or a stage-direction "line" attaches NO ref, so H3 never tries to
    # voice it. Audio refs must accompany image refs. Max 3 clips.
    audio_filenames: list[str] = []
    audio_refs: list[dict[str, str]] = []
    if image_filenames:
        speakers = [d.get("char") for d in _spoken_entries(shot)]
        for name in dict.fromkeys(speakers):
            if not name or len(audio_filenames) >= 3:
                break
            vp = _voice_sample_path(show, name)
            if not vp or name not in names:
                continue
            try:
                audio_filenames.append(client.upload_audio(vp))
            except Exception:
                continue
            audio_refs.append({"char": name,
                               "token": f"<Audio {len(audio_refs) + 1}>"})

    prompt = compile_shot_prompt(show, script, scene, shot, names,
                                 audio_refs=audio_refs or None)
    # Pass 1 (prepare_episode_prompts) already persisted the final prompt — the
    # rewritten brief when h3_rewrite_prompt is on, else the deterministic one.
    # Use it verbatim so this render pass makes ZERO LLM calls (the LLM is long
    # unloaded by now and must not be needed while H3 owns the GPU).
    prepared = _load_prepared_prompts(show, episode)
    entry = prepared.get(shot.get("id", "shot"))
    if entry and isinstance(entry, dict) and entry.get("rewritten"):
        prompt = entry["rewritten"]
    # A human REJECTED this shot with notes: the revised prompt from the Shot
    # gate overrides the prepared one (it incorporates the rejection feedback).
    from .approval import shot_approvals
    shot_entry = shot_approvals(show, episode).get(sid)
    if shot_entry and shot_entry.get("revised_prompt"):
        prompt = shot_entry["revised_prompt"]

    # Sampling: fused 4-step turbo (185s) vs legacy 8-step (406s) at 864x480.
    # When h3.fused.enabled, use the single-file fused checkpoint + SLA + sigma shift.
    h3_cfg = cfg.get("comfy", "h3", {})
    fused_cfg = h3_cfg.get("fused") if isinstance(h3_cfg.get("fused"), dict) else {}
    if bool(fused_cfg.get("enabled", False)):
        from .h3_fused import build_h3_fused_workflow
        f_steps = int(fused_cfg.get("steps", 4) or 4)
        f_sampler = fused_cfg.get("sampler") or h3_cfg.get("sampler") or "res_multistep"
        f_sched = fused_cfg.get("scheduler") or "simple"
        f_width = int(fused_cfg.get("width", 864) or 864)
        f_height = int(fused_cfg.get("height", 480) or 480)
        wf = build_h3_fused_workflow(
            prompt, float(shot.get("duration_s", 10.125)), seed, cfg=cfg,
            ref_image_filenames=image_filenames or None,
            ref_audio_filenames=audio_filenames or None,
            width=f_width, height=f_height,
            steps=f_steps,
            sampler_name=f_sampler,
            scheduler=f_sched,
            use_sla=bool(fused_cfg.get("use_sla", True)),
            sla_sparsity=float(fused_cfg.get("sla_sparsity", 0.9) or 0.9),
            sla_block=int(fused_cfg.get("sla_block", 64) or 64),
            use_sage=bool(fused_cfg.get("use_sage", False)),
        )
    else:
        width = int(h3_cfg.get("width", 864) or 864)
        height = int(h3_cfg.get("height", 480) or 480)
        wf = build_h3_ref2va_workflow(
            prompt, float(shot.get("duration_s", 10.125)), seed, cfg=cfg,
            ref_image_filenames=image_filenames or None,
            ref_audio_filenames=audio_filenames or None,
            width=width, height=height,
            steps=int(h3_cfg.get("steps", 8) or 8),
            sampler_name=h3_cfg.get("sampler") or "res_multistep",
            scheduler=h3_cfg.get("scheduler") or "simple",
            use_spectrum=bool(h3_cfg.get("spectrum", False)),
            use_first_block_cache=bool(h3_cfg.get("first_block_cache", False)),
        )
    out = show.dir / "runs" / f"EP{episode:02d}" / "video" / f"{sid}.mp4"
    return run_h3_shot(client, wf, out, timeout_s=timeout_s)


def _free_gpu(client: ComfyClient) -> None:
    """Best-effort VRAM release; never raise on cleanup."""
    try:
        client.free_memory()
    except Exception:
        log.debug("gpu free failed", exc_info=True)


def render_episode(show: Show, episode: int, cfg=None, progress=None,
                   timeout_s: float = 1800.0, should_stop=None) -> int:
    """Render every shot of the latest episode script to video. Returns shot count.

    The whole episode runs under the GPU manager's exclusive COMFYUI ownership
    (§15.4): acquire() ensures the H3 instance is up, and release() evicts it
    (or, with ``comfy.manage_lifecycle``, shuts it down) so the GPU is never
    left occupied after a render.
    """
    from .gpu_manager import ServiceType, get_gpu_manager

    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    if not script:
        return 0
    client = _render_client(cfg)
    stt_enabled = bool((cfg.get("qc", "stt", {}) or {}).get("enabled", True))
    shots = [s for sc in script.get("scenes", []) for s in sc.get("shots", [])]
    done = 0
    with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
        try:
            for sc in script.get("scenes", []):
                for shot in sc.get("shots", []):
                    if should_stop and should_stop():
                        return done  # cooperative cancel: stop before the next shot
                    sid = shot.get("id", f"sh{done}")
                    out = show.dir / "runs" / f"EP{episode:02d}" / "video" / f"{sid}.mp4"
                    if out.exists():
                        done += 1
                        if progress:
                            progress(done, len(shots), sid)
                        continue
                    ACTIVITY[show.show_id] = {"detail": f"Rendering {sid} (H3 video)…",
                                              "ts": time.time()}
                    try:
                        # A rejected/re-taken shot renders with a fresh seed per
                        # retake (else the SAME prompt+seed reproduces the same
                        # clip and the rejection loops forever).
                        from .approval import shot_approvals
                        retake = int((shot_approvals(show, episode)
                                      .get(sid, {}).get("retake_count", 0) or 0))
                        _render_shot(show, episode, client, cfg, script, sc, shot,
                                     seed=done + 1000 * retake, timeout_s=timeout_s)
                    except Exception as exc:
                        ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                            f"Render {sid} failed: {exc}")
                    # STT dialogue QC: transcribe + compare against the script.
                    # Runs on CPU (never the GPU) so it can't contend with H3 for
                    # VRAM; failures are recorded, never thrown. Only runs when
                    # the video file actually exists (a failed render means no
                    # clip to transcribe).
                    if stt_enabled and out.exists():
                        from .qc import run_shot_qc
                        ACTIVITY[show.show_id] = {"detail": f"QC {sid} (STT dialogue)…",
                                                   "ts": time.time()}
                        try:
                            qc_res = run_shot_qc(show, episode, out, shot, cfg=cfg)
                            # Winning fix: silent hallucination retake (same seed always hallucinates at 42, different seed fixes)
                            # If silent shot hallucinates speech, auto-retry with new seed (seed+1000) up to 2 extra tries
                            if qc_res and qc_res.get("verdict") == "fail" and bool(shot.get("silence")):
                                issues = qc_res.get("issues") or []
                                if any("silent shot has unexpected speech" in str(x) for x in issues):
                                    max_silent_retakes = int(cfg.get("qc", "stt", {}).get("max_silent_retakes", 2) or 2)
                                    for attempt in range(1, max_silent_retakes + 1):
                                        ACTIVITY[show.show_id] = {"detail": f"QC {sid} silent hallucination {len(qc_res.get('transcript','').split())}w → retake {attempt}/{max_silent_retakes} (seed {done + 1000*attempt})…", "ts": time.time()}
                                        # delete failed clip so _render_shot will re-render with new seed
                                        try:
                                            out.unlink(missing_ok=True)
                                        except Exception:
                                            pass
                                        # bump retake count for seed calculation
                                        from .approval import shot_approvals as _sa
                                        # also persist retake_count for dashboard visibility (optional)
                                        try:
                                            _sa_cache = _sa(show, episode)
                                            _sa_cache.setdefault(sid, {})["retake_count"] = int((_sa_cache.get(sid, {}).get("retake_count", 0) or 0)) + 1
                                            # persist via approval module's internal save if available
                                            from .approval import _save_shot_approvals  # type: ignore
                                            _save_shot_approvals(show, episode, _sa_cache)  # type: ignore
                                        except Exception:
                                            pass
                                        try:
                                            _render_shot(show, episode, client, cfg, script, sc, shot,
                                                         seed=done + 1000 * attempt, timeout_s=timeout_s)
                                        except Exception as exc2:
                                            ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Retake {sid} failed: {exc2}"
                                            continue
                                        if out.exists():
                                            try:
                                                qc_res2 = run_shot_qc(show, episode, out, shot, cfg=cfg)
                                                if qc_res2.get("verdict") != "fail":
                                                    qc_res = qc_res2
                                                    ACTIVITY[show.show_id] = {"detail": f"QC {sid} retake {attempt} passed ({len(qc_res.get('transcript','').split())}w)", "ts": time.time()}
                                                    break
                                                qc_res = qc_res2
                                            except Exception as exc2:
                                                ACTIVITY.setdefault(show.show_id, {})["detail"] = f"QC retake {sid} failed: {exc2}"
                        except Exception as exc:
                            ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                                f"QC {sid} failed: {exc}")
                    done += 1
                    if progress:
                        progress(done, len(shots), sid)
        finally:
            # Always release the GPU after an episode, success or failure.
            _free_gpu(client)
    return done


def build_render(show: Show, episode: int, cfg=None) -> None:
    """Render all shots in a background thread with progress.

    When config.multishot.enabled is true, delegates to render_scene (one
    seamless master per scene via H3Multishot). Otherwise renders per-shot
    via the legacy ReferenceToVideo loop.

    Gated on the same ref-approval set as the storyboard previews: a final
    render must not use a costume variant or recurring-object ref the human
    hasn't approved. Pending refs -> the job parks in "waiting" instead of
    burning GPU on an unapproved identity.

    The job tracks its worker ``_thread`` and a ``ts`` last-progress stamp so
    the episode reconciler can detect a zombie (thread died) or a hung render
    (no progress for a long time) and restart it instead of waiting forever.
    """
    cfg = cfg or get_config()
    # A render is expensive; never start it against an unapproved/stale outline.
    from .storyboard import storyboard_blockers
    blockers = storyboard_blockers(show, episode)
    if blockers:
        RENDER_JOBS[show.show_id] = {
            "state": "blocked", "done": 0, "total": 0, "ts": time.time(),
            "detail": "Cannot render: " + "; ".join(blockers)}
        return
    ms_cfg = cfg.get("pipeline", "multishot", {}) if isinstance(cfg.get("pipeline", "multishot", {}), dict) else {}
    if bool(ms_cfg.get("enabled", False)):
        from .render_scene import build_scene_masters as _build_scene
        return _build_scene(show, episode, cfg=cfg)
    from .storyboard import pending_ref_approvals
    pending = pending_ref_approvals(show, episode)
    if pending:
        RENDER_JOBS[show.show_id] = {
            "state": "waiting", "done": 0, "total": 0, "ts": time.time(),
            "detail": "Waiting for ref approval: " + ", ".join(pending),
        }
        return

    job: dict[str, Any] = {"state": "running", "done": 0, "total": 0,
                           "detail": "Preparing render…", "ts": time.time()}
    RENDER_JOBS[show.show_id] = job

    # Pass 1 (LLM phase): compile + (optionally LLM-rewrite) EVERY shot prompt
    # and persist to prompts.json, while the LLM owns the GPU. When this returns,
    # the LLM is fully unloaded so H3 (Pass 2) gets the whole GPU and never
    # competes with it for VRAM. No LLM calls happen during the render itself.
    def _prepare_prog(done, total, label):
        job["detail"] = f"Writing prompts {done}/{total}: {label}"
        job["ts"] = time.time()
        ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}

    script = _latest_script(show, episode)
    total = len([s for sc in (script or {}).get("scenes", [])
                 for s in sc.get("shots", [])])
    job["total"] = total
    try:
        prepared = prepare_episode_prompts(show, episode, cfg=cfg,
                                           progress=_prepare_prog)
    except Exception as exc:
        job["state"] = "failed"
        job["detail"] = f"Prompt preparation failed: {exc}"
        ACTIVITY.pop(show.show_id, None)
        return
    job["prepared"] = prepared

    def _run():
        def prog(done, total, label):
            job["done"], job["total"] = done, total
            job["detail"] = f"Rendering {done}/{total}: {label}"
            job["ts"] = time.time()
            ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}

        try:
            job["state"] = "running"
            done = render_episode(
                show, episode, cfg=cfg, progress=prog,
                should_stop=lambda: render_status(show.show_id).get("state") == "stopped")
            # Post-render visual check (Qwen3-VL): the episode render has
            # released the COMFYUI lease, so the VLM on the GPU is free to run.
            # Frames from each clip are reviewed against the shot's character
            # refs + scripted action; fails auto-feed the shot gate (capped)
            # and delete the clip, which re-kicks the render loop below.
            vk = cfg.get("qc", "vcheck", {}) or {}
            if vk.get("enabled") and vk.get("schedule", "post_render") == "post_render":
                from .vcheck import run_episode_vcheck

                def vprog(d, t, label):
                    job["detail"] = f"Visual QC {d}/{t}: {label}"
                    job["ts"] = time.time()
                    ACTIVITY[show.show_id] = {"detail": job["detail"],
                                              "ts": time.time()}

                try:
                    run_episode_vcheck(show, episode, cfg=cfg, on_progress=vprog)
                    job["detail"] = f"Render complete ({done}/{total} shots); visual QC done"
                except Exception as exc:
                    job["detail"] = f"Render complete ({done}/{total} shots); visual QC failed: {exc}"
            else:
                job["detail"] = f"Render complete ({done}/{total} shots)"
            job["state"] = "done"
        except Exception as exc:
            job["state"] = "failed"
            job["detail"] = f"Render failed: {exc}"
        finally:
            job["ts"] = time.time()
            ACTIVITY.pop(show.show_id, None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    job["_thread"] = t


def stop_render(show_id: str) -> None:
    """Mark the render job stopped so a running loop halts at the next shot."""
    job = RENDER_JOBS.get(show_id)
    if job:
        job["state"] = "stopped"
        job["detail"] = "Stopped by user"


def render_status(show_id: str) -> dict[str, Any]:
    job = RENDER_JOBS.get(show_id)
    if not job:
        return {"state": "idle", "done": 0, "total": 0, "detail": ""}
    out = dict(job)
    thread = out.pop("_thread", None)
    out["alive"] = bool(thread and thread.is_alive())
    return out
