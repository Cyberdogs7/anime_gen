# HANDOFF — Anime Studio Pipeline

Handoff for a new agent working on `C:\Users\Chad\PycharmProjects\anime` (a.k.a. the "anime studio").

This is an **endless, plotline-driven anime series production pipeline**. It bootstraps a show
(concept → bible → characters → scenes), produces full-length episodes in a **chunked
plan → scene-details → per-scene-shots** flow, renders a visual **storyboard** (per-shot
keyframes via ComfyUI Krea 2), and enforces **character consistency** with a vision-LLM
reviewer that rewrites prompts and regenerates. A human approval dashboard drives every gate.

---

## 1. Machines / infrastructure

| Node | Role | What runs there |
|------|------|-----------------|
| **BEAST5** (this machine) | Renderer / controller | Anime repo, H3 ComfyUI `D:\anime-h3\ComfyUI` (127.0.0.1:8188), local LM Studio (127.0.0.1:1234), dashboard (127.0.0.1:8125), LanguageLearner krea2 ComfyUI (transient, 127.0.0.1:8189) |
| **Beast3** (192.168.50.173) | Worker | LM Studio `C:\anime-studio\portable\lmstudio` (127.0.0.1:1234; **portproxy `0.0.0.0:1235 → 127.0.0.1:1234`**), krea2 ComfyUI (127.0.0.1:8188; **portproxy `0.0.0.0:8189 → 127.0.0.1:8188`**) |

- **WinRM** creds cached at `%TEMP%\beast3-cred.xml` (use `New-PSSession`).
- **The LLM is the gemma model on Beast3**, reached from BEAST5 via `http://192.168.50.173:1235/v1` (`config/llm.yaml`).
- **LM Studio server on Beast3 must be started via the `LMStudioServerStart` scheduled task** — an ad-hoc `lms server start` dies when its session closes. Load the model with `lms load "gemma-4-e4b-uncensored-hauhaucs-aggressive" --context-length 65536 -y`.

### Models
- **LLM**: `gemma-4-e4b-uncensored-hauhaucs-aggressive` (Beast3, 65536 ctx). It is a **reasoning/multimodal model** — vision works but needs `max_tokens` ~1500+, or it burns tokens on `reasoning_content` and returns empty.
- **Image**: Krea 2 ComfyUI. Primary = BEAST5 LanguageLearner krea2 (started **on demand** only when H3's queue is empty, then shut down); fallback = Beast3 krea2. `_pick_krea2()` in `studio/remote/ops.py` handles this.
- **Voice**: Qwen3-TTS via LanguageLearner venv (`config/env.yaml` → `tts.*`, includes `sox` path).

---

## 2. Pipeline stages (as built)

### Gate 0 — Bootstrap (`studio/bootstrap.py`, `studio/approval.py`)
`concept → bible → characters → scenes`, each an approve/reject gate. State in `shows/<id>/bootstrap.json`.
- **Characters** are the combined-gate model: each character gets a **proposal + Krea 2 ref image + Qwen3-TTS voice sample**, all generated together, then ONE approve. Per-asset rejects exist: **Reject image / Reject voice** regenerate only that asset (LLM rewrite via `revise_character_appearance` / `revise_voice_description` in `bootstrap.py`).
- **Scenes** get location ref images (non-gating).

### Episode — chunked (`studio/planner.py`)
This is the correct full-length-episode path (single-pass `WritersRoom.run` in `scriptgen.py` produces too-short scripts — see Issues).

1. **Story engine template** — an architect LLM (`prompts.story_engine_architect_prompt`) reads the show's bible and writes a **show-specific** "Lead Story Engine" prompt template with `{{PLACEHOLDER}}` tokens. Stored at `shows/<id>/story_engine.txt`, generated lazily on first outline generation. **The pipeline stays generic** — each show gets its own tailored engine.
2. **Outline (Stage 1)** — the stored template is filled with live state (`fill_outline_template`) and sent; output is `{episode, title, threat_of_the_week, plot, characters, plotline_updates}` → `runs/EP##/plan.json` (status pending). **Approve / Reject** in the Story tab. Rejection uses a separate **REVISE** prompt with your notes (does NOT edit the bible).
3. **Scene details (Stage 2)** — scene breakdown from the outline, then per-scene detailed treatments (`runs/EP##/scene_details.json`, pending). **Approve / Reject scenes**.
4. **Per-scene shots (Stage 3)** — each approved scene's shots written with localized context, then assembled → `runs/EP##/script.r1.json`. A **runtime check** (`_runtime_review`) fails scripts under 80% of `runtime_target_s`.

### Storyboard (`studio/storyboard.py`)
Per-shot keyframes via Krea 2 + IPAdapter, all in a background thread (`/api/show/<id>/storyboard`):
- **Ref pass** (`studio/casting.py`) — new characters from the script's structured `cast` get a ref image.
- **Costume variants** — `references.costumes` per shot; `ensure_variant_refs` generates a ref per (character, costume) pair; `_shot_refs` picks the declared variant.
- **IPAdapter** — chained per-character refs (multi-image batch fails on this stack; chaining works). Shot-line continuity: the previous keyframe in a scene is added as an extra ref.

### Consistency (`studio/consistency.py`)
Automatic after storyboard:
- Vision review per shot (strict: every expected character present+consistent, **unknown-character detection**, **text-defect detection**).
- Failures → **LLM REWRITES the whole keyframe prompt** (`revise_keyframe_prompt`) → regenerate → re-review only the failures → auto-iterate to convergence (default `max_rounds=4`).

### Dashboard (`studio/dashboard.py` + `dashboard.html`)
- Setup tab (bootstrap gates, per-character approve/reject/reject-image/reject-voice).
- Story tab: episode outline → scene details → storyboard (visual keyframes) → consistency verdicts. Live **activity bar** (token counts) + **streamed output panel** (bottom) + per-episode **status line** (generating / flagged / awaiting approval / approved).
- Data is rendered **human-readable** (labels, chips, nested groups) — no raw JSON on Setup.

---

## 3. Key files

- `studio/bootstrap.py` — Gate-0 chain + character proposal/refs/voice + revision helpers.
- `studio/approval.py` — approve/reject for all gates (bootstrap + story).
- `studio/development.py` — plotline selection (`develop_episode`, `_plotline_state`, `_record_seen`).
- `studio/scriptgen.py` — single-pass writers' room (`WritersRoom.run`), `_normalize`, `_runtime_review`.
- `studio/planner.py` — **chunked episode**: story engine, outline, scene details, assemble, state tracker; per-scene shot pass chain (blocking → camera → action → references → costumes → soundscape → dialogue).
- `studio/storyboard.py` — keyframes, `_char_ref_map`, `_shot_refs`, `ensure_variant_refs`, shot-line refs.
- `studio/casting.py` — ref pass for new characters (diff of script `cast` vs approved refs).
- `studio/consistency.py` — vision reviewer, prompt rewrite, regenerate, converge.
- `studio/approval.py` — **Shot gate (Gate 4)**: per-shot approve/reject-with-notes on rendered clips.
  State in `runs/EP##/shots.approval.json`. A reject LLM-revises the shot's render prompt from the
  notes (persisted so successive rejects refine it), bumps `retake_count`, deletes the clip + QC
  entry; the reconciler re-renders ONLY that shot with the revised prompt and a fresh seed
  (`seed = done + 1000*retake`). `render.py` `_render_shot` prefers the stored `revised_prompt`.
  Dashboard: per-shot "Approve" / "Reject with feedback" buttons + retaking badge + reject notes.
- `studio/vcheck.py` — **post-render visual check (Qwen3-VL)**: verifies every rendered clip's
  characters match their approved refs AND the scripted action actually plays out. Two serving
  paths (`config/qc.yaml → vcheck.server`):
  - **`lmstudio`** (default, and the ONLY reliably-working path on BEAST5): frame-sampling via the
    local `qwen/qwen3-vl-8b` GGUF. LM Studio's OpenAI endpoint accepts **text + images only** —
    it rejects `video_url` (`400: content objects must have a 'type' field that is either 'text'
    or 'image_url'`). Frames are spread evenly across the clip (`frames`, default 8) and
    downscaled to `frame_sample_dim` (default 512) so more of the motion fits the context window.
    The model's load/unload is **managed by the GPU lock**: `run_episode_vcheck` acquires the VLM
    GPU lease (`ServiceType.VLM`), which `lms load qwen/qwen3-vl-8b` on entry and `lms unload
    --all` on exit — the model never has to be resident by hand, and VRAM is freed when QC
    finishes. Verified working end-to-end against the live server.
  - **`vllm`** (native whole-clip video in WSL2): BLOCKED on this machine. `_load_vllm`'s launch
    command was fixed (vLLM's module launcher takes `<model>` positionally, not `serve <model>`),
    but the FP8 checkpoint cannot start in WSL2 — `RuntimeError: UVA is not available`. Qwen3-VL
    FP8 needs Unified Virtual Addressing, which NVIDIA's WSL2 driver does not expose. Only usable
    if pointed at a UVA-capable host (Windows-native vLLM on the RTX 4060, or another box).
  Reports live in `runs/EP##/vcheck/summary.json` + the `vcheck_results` DB table; VL
  badges/rollup on the dashboard. Runs automatically after an episode render completes (the render
  job calls `run_episode_vcheck` post-render, when the GPU is free). Fails auto-feed the Shot gate
  (`reject_shot` from the VL issues) capped by `vcheck.max_auto_retakes` → prompt revision + retake.
  Backfill: `studio.py vcheck --show <id>[ --no-auto-feed]`.
  NOTE: `_load_vllm` spawns the WSL server via `wsl -d <distro> -- bash -lc "nohup ..."`; a
  transient `wsl` call does not keep a backgrounded child alive, so if the native path is ever
  enabled a **systemd unit** (e.g. `/etc/systemd/system/vllm-qwen3vl.service`) is the reliable
  way to run it.
- `studio/qc.py` — **STT dialogue QC (garbled-audio detection)**: transcribes every rendered clip
  with faster-whisper (CPU, never GPU) and compares against the shot's script dialogue. Reports
  live in `runs/EP##/qc/summary.json` + the `qc_results` DB table; per-shot verdicts show on the
  dashboard. Config under `config/qc.yaml → stt` (model, thresholds). Backfill already-rendered
  clips with `studio.py qc --show <id> --episode <n>`. Calibrated on blade-s-bedlam EP01: clean
  clips explain 0.81–1.00 of their transcript, garbled clips 0.29–0.64 → `min_explained: 0.70`.
  Auto-runs inline after each shot in `render_episode` (config `qc.stt.enabled`).
- `studio/prompts.py` — ALL prompts incl. `story_engine_architect_prompt`, `scene_breakdown_prompt`, `scene_detail_prompt`, `scene_pass_prompt` (per-scene shot passes), `episode_plan_prompt` (REVISE), `development_prompt`.
- `studio/comfy_workflows.py` — krea2 workflow builders incl. IPAdapter chaining.
- `studio/clients/{lmstudio,comfy,tts,ffmpeg}.py` — clients (LM Studio streaming, Comfy uploads, Qwen3-TTS, ffprobe).
- `studio/remote/ops.py` — `_pick_krea2` (transient launch / idle-guard / fallback), `_krea2_client`.
- `studio/dashboard.{py,html}` — HTTP dashboard + UI.
- `studio/config.py`, `studio/show.py` — config + show data model.
- `config/*.yaml` — llm (base_url/model/roles/context), env (comfy urls, tts), comfy, show_profile.

Data under `shows/<id>/`: `bible.yaml`, `concept.json`, `bootstrap.json`, `story_engine.txt`,
`director_notes.json`, `characters/`, `scenes/`, `voices/`, `assets/voice/`,
`runs/EP##/{plan.json, scene_details.json, script.r*.json, storyboard/, objects/, reviews/, consistency.json}`.

---

## 4. Current show state (`blade-s-bedlam`)

- **Reset to concept + bible only** (per user request). Characters/scenes/voices/runs/engine/refs all deleted; bootstrap keeps concept+bible approved.
- **Blade regenerated**: proposal + ref image + voice sample, blocked for approval. The other four cast members (Lily, Ivy, Rose, Dahlia) generate as Blade is approved.
- `story_engine.txt` was deleted in the reset → will regenerate on the first outline generation.
- `director_notes.json` deleted.

---

## 5. Known issues / decisions (read before touching anything)

- **Native video → Qwen3-VL is blocked on BEAST5.** LM Studio's endpoint accepts images only (rejects `video_url`), and WSL vLLM can't load the FP8 checkpoint (`UVA is not available` in WSL2). The render-clip QC therefore runs as **frame-sampling** through the local `qwen/qwen3-vl-8b` GGUF (`vcheck.server: lmstudio`) — this is the intended, working config. If native video is ever required, it must run on a UVA-capable host.
- **Single-pass scripts are too short.** `WritersRoom.run` writes ~15 shots (~2 min) even with 65536 ctx and a length demand. The **chunked planner is the real path**; keep using it.
- **Context poisoning.** The bible's plotline summaries ("Lily's Rivalry … compete for Blade", "four mercs fight for his affection") get injected verbatim and make the LLM produce infighting during battles. Mitigation: the show-specific story engine's **CRITICAL RULE #1 (Combat Purity)** + `director_notes`. Per user decision, **rejections do NOT auto-edit the bible** (the `revise_bible_for_director` behavior was removed).
- **Director notes** (`director_notes.json`) currently reach the **Development** prompt only; they are NOT injected into the outline creation prompt (the template's rules are static from the architect). If a user's standing note must shape outlines directly, wire `read_director_notes` into the outline prompt or into the story-engine template.
- **IPAdapter multi-image batch fails** on the krea2 stack → use the **chained per-ref** builder (`build_keyframe_ref_workflow`). Multi-character shots still steer mostly from the first ref; per-character region control is future work.
- **gemma at 65536 ctx on Beast3's 6 GB card** → KV offload, slow generation. Expect minutes per large prompt.
- **LM Studio** must be started via the scheduled task; `lms load --context-length 65536`.
- **Negative-prompt contamination**: once a revision says "instead of X", the model echoes "instead of X". Clean the SOURCE (plotlines/template) rather than negating.
- **PowerShell quirk**: `Start-Process` with `-RedirectStandardOutput` often returns `Unknown: ChildProcess.kill`; verify the process actually started (port check).
- **Stale script files**: `_latest_script` now sorts by mtime (not filename) so old higher-round files can't shadow a regeneration.
- **ALL reject-with-notes feedback MUST follow the proper loop**, not raw string-appending. The correct pattern (see the costume/char-ref/object loops): take the artifact's **current stored generation prompt**, LLM-revise it from the rejection notes (`revise_*_prompt` in `prompts.py` + the showrunner), **persist the revised prompt** keyed to the artifact so successive rejects FURTHER refine it instead of resetting to the base template, then re-render. Never just do `prompt += " Adjust: {notes}"` and re-render — that ignored feedback (the local model treated it as a fragmented tail) and discarded prior corrections every iteration. Object prompts persist to `runs/EP##/objects/prompts.json`; costume prompts live in `refs.json["prompts"]`; char appearance in `appearance_canonical`. Before wiring a new reject/regenerate path, mirror `regenerate_object_ref` / `regenerate_character_ref` / the costume reject branch in `studio/approval.py`, and add a test asserting the revised prompt is stored and reused.

---

## 6. What remains / next steps

1. **H3 video rendering** — the final stage. The storyboard/consistency produces the reviewable per-shot input; rendering shots into video via H3 ComfyUI is not wired end-to-end yet.
2. **STT QC automation** — `studio/qc.py` runs per-shot dialogue verification (garbled-audio detection) after each render and records `qc_results` + `runs/EP##/qc/summary.json`. Next: auto-retake flagged shots (or route them to the human Shot gate) instead of only surfacing them on the dashboard.
3. **Writers' room review on chunked scripts** — the chunked assemble writes `script.r1.json` but doesn't run the slop/continuity/fan-service review loop. Apply `run_reviewers`/`revision_prompt` to chunked scripts.
4. **Object/recurring-prop consistency** — object refs are generated and shown but not injected into keyframes via IPAdapter yet.
5. **Per-character IPAdapter region control** for true multi-character consistency.
6. **Continuity state population** — the state tracker reads `last_seen_episode` from continuity; make sure `develop_episode`/`_record_seen` updates it each episode.
7. **Director notes → outline prompt** (see Issues) if standing notes must drive outlines directly.
8. **Git**: repo on `main` at `github.com/Cyberdogs7/anime_gen`. The initial commit is pushed; **the large body of later work is committed locally but not yet pushed** — the user pushes when ready (never push to prod).

---

## 7. Useful commands

```
# board (API + daemon)
.venv\Scripts\python.exe studio.py board --port 8130

# tests
.venv\Scripts\python.exe -m pytest tests -q

# backfill STT dialogue QC on already-rendered clips (also auto-runs per shot during render)
.venv\Scripts\python.exe studio.py qc --show <show-id> --episode 1

# post-render visual QC (Qwen3-VL); native video via WSL vLLM by default
.venv\Scripts\python.exe studio.py vcheck --show <show-id> --episode 1

# vLLM runtime (Qwen3-VL-8B) lives inside WSL2 (Ubuntu-22.04):
#   ~/vllm-venv/bin/vllm serve ~/models/Qwen3-VL-8B-Instruct-FP8 --port 8001 ...
# Started/stopped automatically by the GPU manager (ServiceType.VLLM) around each
# post-render vcheck pass; never leave it running during an H3 render.

# run the bootstrap chain (advance gates)
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from studio.show import Show; from studio.bootstrap import BootstrapChain; print(BootstrapChain(Show('<show-id>')).advance())"

# Beast3 LM Studio
$cred = Import-Clixml "$env:TEMP\beast3-cred.xml"; $s = New-PSSession -ComputerName 192.168.50.173 -Credential $cred
Invoke-Command -Session $s -ScriptBlock { & "C:\anime-studio\portable\lmstudio\resources\app\.webpack\lms.exe" ps }
Start-ScheduledTask -TaskName "LMStudioServerStart"  # to restart the server
```
