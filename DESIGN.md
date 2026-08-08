# Automated Anime Show Creator — Design & Specification

**Status:** Draft v0.1  
**Date:** 2026-08-06  
**Owner:** Chad  
**Goal:** A fully automated, local-first "studio in a box" that produces a new episode of the user's custom anime every night. The show is **AI-generated** — the user answers a one-line brief and clicks through the approval gates (show, characters, voices, episode) — then the pipeline runs unattended and the user wakes up to a finished episode plus a QC report.

---

## 1. Executive summary

The system is a pipeline of autonomous "studio departments" that mirrors a real anime production:

| Real studio role | System component |
|---|---|
| Writer's room / showrunner | **Showrunner LLM** (LM Studio, local) |
| Character designer | **Krea 2** (local ComfyUI checkpoint) |
| Keyframe / storyboard artist | **Krea 2** + orchestrator (first/last frame anchors) |
| Shot lead / animation | **MiniMax H3** via **ComfyUI-MiniMaxH3-Director** |
| Voice director | **TTS engine(s)** — per-character voices, pre-generated lines |
| Sound designer | H3 joint audio + TTS dialogue mux |
| Editor / conform | **ffmpeg** assembler |
| QC | Automated QC + retake loop (H3 Retake Stitch) |
| Studio manager | **Run controller + event bus** + registry |
| Studio records | **SQLite** (continuity DB, asset registry, run reports) |

All generative models run **locally**. No hosted API is in the generative path, which is what makes the content-policy requirement viable: prompts the user flags as "mature" never leave the machine.

**Hands-off by design:** the show itself is **AI-generated**. The Showrunner proposes the bible, cast, voices, and initial scenes from a one-line brief (or even zero input); the human's job is *input and approval*, not authoring — review/approve the proposal (Gate 0), the character refs (Gate 1), the voices (Gate 2), and the finished episode (Gate 5). Everything between the gates runs unattended (see §9.5, §24).

**Reality check baked into the design:** MiniMax H3 renders one **4–15 s** clip per job. A 22-minute episode is ~140 shots. At local render speeds this is many hours of GPU time, so the pipeline is designed to be **resumable, checkpointed, and multi-night**: each scheduled run does as much work as the nightly window allows, picks up exactly where it left off, and the episode "lands" when complete. See §15 for the honest throughput model and the three operating modes.

---

## 2. Naming / model identity corrections

- **"Alibaba H3"** → The linked repo is **MiniMax H3** (a MiniMax model, packaged for ComfyUI by Comfy-Org; HuggingFace: `Comfy-Org/MiniMax-H3`). Alibaba's open video models are the **Wan 2.x** family. This design targets **MiniMax H3** per the referenced repo. If the user actually wants Wan, the H3-specific sections (joint AV, 17k+5 grid, prompt notation) must be re-specced.
- **"Ker2" / "Krea 2"** → The image model is **Krea 2**, run as a **local ComfyUI checkpoint** (per the architecture decision in the kickoff Q&A — not Krea's hosted API). The exact checkpoint filename and the image workflow JSON are configuration, not code; §10 shows how to plug it in. The user must confirm the checkpoint file + preferred aspect/sampler. (If Krea 2 only ships as a hosted API in their setup, §10.1 must be re-specced to an HTTP client instead of a ComfyUI workflow — flagged as open question #1.)

---

## 3. Goals and non-goals

### Goals
1. **Event-driven** episode production, unattended. A durable event bus (§6, §6.4) carries all pipeline work — a nightly *trigger* just publishes one `RunRequested` event; every department is a consumer that reacts. No monolithic cron script.
2. **AI-generated creative with human approval, not human authoring.** The Showrunner writes the bible, cast, voices, scenes, and every script from a short brief; the human reviews and approves/rejects at the six gates (§9.5). The default is *as hands-off as possible* — a human can run the whole pipeline by answering one brief and clicking four approvals.
2. Persistent **show continuity** (characters, world state, plotlines, unresolved threads) across episodes.
3. Strong **character identity consistency** across shots *and* episodes (reference banks + keyframe anchoring).
4. **Local-only generation** — image gen, video gen, LLM, TTS all local. No cloud dependency for the content path.
5. Mature/adult content supported **by design**, with an explicit, configurable content-policy guardrail that runs locally (§13).
6. Crash-safe, resumable pipeline; every event handler idempotent (deduped by `event_id`), every stage logged, resume = stream replay + DB state.
7. Per-character pre-generated dialogue fed into H3 as audio references, with an ffmpeg mux fallback.
8. **Multi-show, infinite-run studio** — any number of shows (`shows/<show_id>/`), each with its own canon; the studio produces **until the user stops it** and is **live-appendable** (add characters/themes while it runs, §9.5b).

### Non-goals (v0)
- Dedicated/external lip-sync post-processing (e.g. a Wav2Lip-style stage). Not needed in v0: because H3 generates video and audio jointly, feeding dialogue in as a reference audio clip conditions the video on that same audio latent, so characters produce mouth motion in-model while H3 renders. Lip-sync is therefore an **expected property of the `h3_reference` audio path**, not a separate stage — QC verifies it (see §12.3); we only fall back to ffmpeg dialogue muxing when reference-audio limits or quality force it.
- Real-time / interactive generation.
- Public distribution or monetization features.
- Multi-GPU auto-scaling beyond the two-node topology (§15.5).
- A hosted web platform (optional local dashboard only).

---

## 4. Requirements

### 4.1 Functional
- FR-1 **Event-triggered nightly production:** a timer source publishes `RunRequested` on the bus (§6.4); no monolithic scheduler. Optional Windows Task Scheduler exists only as an alarm clock that fires that one event.
- FR-2 Show configuration in per-show folders under `shows/<show_id>/` (bible, characters, voices, style, policy).
- FR-3 Episode produced through the 9-stage pipeline (§9), stages as **event consumers**, with per-stage state persistence.
- FR-4 Shots render through ComfyUI H3 Director with first/last keyframes and reference images; dispatch happens via `ShotScheduled` events consumed by a renderer agent.
- FR-5 Character voices generated via configured TTS engines and fed to H3 as audio references.
- FR-6 Final episode assembled as one MP4 (H.264/H.265) with mixed audio track.
- FR-7 QC report generated nightly; `ShotQcFailed` events auto-requeue retakes within budget.
- FR-8 Continuity state updated after each completed episode and consumed by the next.
- FR-9 Dashboard (phase 2) to review shots, QC, and approve/reject before "airing" (reacts to the same events).

### 4.2 Non-functional
- NFR-1 **Throughput:** ≥ one 22-min episode per ~2–3 nights on a single mid/high-end GPU; **~1 episode/day** in two-node continuous mode (4060 Ti + 1660 Super, §15.5); see §15.
- NFR-2 **Resumability:** crash at any point resumes from the last committed stage in < 5 min.
- NFR-3 **Local-only:** zero generative calls to external hosts. Network egress blocked by default for pipeline processes (optional firewall rule).
- NFR-4 **Determinism of data:** every generated asset has a registry entry (id, source stage, prompt hash, seed, checksum, status).
- NFR-5 **Auditability:** per-run JSON report + structured logs in `runs/<episode>/`.
- NFR-6 **Idempotency:** re-running a stage with the same inputs produces no duplicate work or assets.
- NFR-7 **Containment:** the pipeline must never modify a show's YAML config (`shows/<id>/`); it only reads it and writes artifacts under `runs/`, `assets/`, `archive/`.

### 4.3 Content policy requirements (mature-content support)
- CP-1 A `content_policy.yaml` defines maturity mode and a blocklist (default: no real persons, no minors, no non-consensual framing; everything else allowed in Mature mode).
- CP-2 Every creative text artifact is scanned by a **local judge LLM** before any image/video generation; violations hard-stop the stage with a report.
- CP-3 No content-policy or prompt text is ever transmitted off-machine.
- CP-4 All generated characters are explicitly fictional (a "fictional characters only" directive in the Showrunner system prompt and enforced by the judge).

---

## 5. Production model mapping (studio metaphor)

```
DEVELOPMENT  →  PRE-PRODUCTION  →  PRODUCTION  →  POST  →  DELIVERY  →  CONTINUITY
```

| Stage | Studio name | System stage | Output |
|---|---|---|---|
| 0 | Production office | Event source | `RunRequested` ticket on the bus |
| 1 | Development | Episode Development | `episode_plan.json` + updated synopsis |
| 2 | Script | Script writing | `script.json` (scenes, shots, dialogue) |
| 3 | Storyboard & keyframes | Keyframe production | Keyframe images + shot prompts + shot list |
| 4 | Continuity check | Pre-render validation | Validated shot list (limit-checked, grid-snapped) |
| 5 | Animation | Shot production (H3) | Per-shot MP4 + joint audio |
| 6 | VO / sound | Dialogue production | Per-shot TTS dialogue audio |
| 7 | Editing / conform | Assembly (ffmpeg) | `episode.mp4` + audio mix |
| 8 | QC / retakes | QC & retake | QC report, retake list, final cut |
| 9 | Distribution | Delivery | Archived episode, playlist update, continuity update |

Each stage is a pure function of (inputs, state) → (outputs, new state), fully resumable.

---

## 6. High-level architecture

### 6.1 Component diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│  EVENT SOURCE: tiny timer producer (in-process APScheduler or Task      │
│  Scheduler alarm) publishes:  RunRequested {episode, mode, budget}      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    ▼
                        ┌──────────────────────┐
                        │   EVENT BUS (Redis   │  durable streams +
                        │   Streams, LAN)      │  consumer groups + DLQ
                        └───┬──────┬──────┬────┘
        ┌───────────────────┘      │      └──────────────────┐
        ▼                          ▼                         ▼
┌──────────────────┐   ┌──────────────────┐   ┌───────────────────────┐
│  STAGE CONSUMERS │   │  RENDERER AGENT  │   │  DASHBOARD / NOTIFIER │
│  (worker node B) │   │  (renderer node  │   │  (node B, phase 2)    │
│  dev · script ·  │   │   A, H3 only)    │   │  reacts to events     │
│  keyframes ·     │   │  consumes        │   │                       │
│  dialogue ·      │   │  ShotScheduled → │   │                       │
│  assembly ·      │   │  renders via     │   │                       │
│  qc · delivery   │   │  ComfyUI → emits │   │                       │
│  LM Studio · TTS │   │  ShotRendered /  │   │                       │
│  ffmpeg · Krea 2 │   │  ShotFailed      │   │                       │
└─────────┬────────┘   └────────┬─────────┘   └──────────┬────────────┘
          │                    │                         │
          ▼                    ▼                         ▼
   SQLite (run state,    SMB artifact      episode wall / toasts
   registry, continuity) store (both nodes)
```

### 6.2 Data flow (per episode)

The 9-stage pipeline is expressed as **event choreography** (every arrow is a bus event, §6.4):

```
RunRequested → [dev] PlanReady → [script] ScriptReady → [keyframes] KeyframesReady
   → [validate] ShotListReady → ShotScheduled×N → (renderer agent A: ShotRendered/ShotFailed)
   → DialogueReady×N → AssemblyRequested → [assemble] AssemblyReady
   → [qc] QcPassed | ShotQcFailed (→ RetakeRequested → ShotScheduled) → EpisodeDelivered → ContinuityUpdated
```

State (the only source of truth for "where are we") still lives in SQLite; the bus is the *work signal*, not the ledger.

### 6.3 Concurrency model

- Stages 1–4 are single consumers and cheap (LLM + small images).
- Stage 5 (H3) is the bottleneck. `ShotScheduled` events feed a **renderer agent pool** (default 1); scale by adding consumers or a second ComfyUI instance (each needs its own GPU/VRAM partition). See §15.3.
- Keyframe gen (Stage 3) runs as its own consumer ahead of H3, so the two heavy checkpoints are never resident at once on a node (§15.4 resource plan).
- **Two-node mode (§15.5):** the worker node B runs the stage consumers (dev/script/keyframes/dialogue/assembly/qc/delivery + LLM + TTS + Krea 2); the renderer node A runs a *renderer agent* that consumes `ShotScheduled` and drives ComfyUI/H3. Keyframes/dialogue for shot *N+k* are produced on B while A renders shot *N* (producer-consumer), so A's H3 GPU stays saturated. Artifacts move over a shared SMB store; the bus is the control plane.

### 6.4 Event bus specification

**Transport (default):** Redis Streams — durable, supports consumer groups (at-least-once), works across the two nodes on LAN, single lightweight broker. Alternatives (config-swappable in `bus.yaml`): NATS JetStream, RabbitMQ AMQP. Broker host: **worker node B** (`127.0.0.1:6379`), reachable by renderer A over LAN with auth.

**Streams / topics:**

| Stream | Key events |
|---|---|
| `bus:run` | `RunRequested`, `RunStarted`, `RunProgress`, `RunAborted`, `RunCompleted` |
| `bus:dev` | `PlanReady`, `PlanFailed` |
| `bus:script` | `ScriptReady`, `ScriptReviewRequested`, `ScriptNotesReady{reviewer}`, `ScriptRevisionRequested`, `ScriptRevised`, `ScriptFailed` |
| `bus:keyframes` | `KeyframesReady{shot_id}`, `RefBankReady`, `KeyframesFailed{shot_id}` |
| `bus:shots` | `ShotScheduled{shot_id, payload_ref}`, `ShotClaimed`, `ShotRendered{asset_ref}`, `ShotFailed{reason}`, `RetakeRequested{shot_id, reason}`, `ShotSkipped` |
| `bus:dialogue` | `DialogueReady{shot_id, asset_ref}`, `DialogueFailed{shot_id}` |
| `bus:assembly` | `AssemblyRequested`, `AssemblyReady`, `AssemblyFailed` |
| `bus:qc` | `QcStarted{shot_id}`, `ShotQcPassed`, `ShotQcFailed{reason}`, `QcReportReady` |
| `bus:approval` | `ConceptPending/Approved/Rejected`, `BiblePending/Approved/Rejected`, `CharacterProposalPending/Approved/Rejected{char}`, `SceneRegistryPending/Approved/Rejected`, `CharacterRefsPending/Approved/Rejected`, `VoiceSamplePending/VoiceApproved/VoiceRejected`, `PlanPending/PlanApproved/PlanRejected`, `ScriptPending/ScriptApproved/ScriptRejected`, `ShotPending/ShotApproved/ShotRejected`, `EpisodePending/EpisodeApproved/EpisodeRejected` (§9.5/§9.5a) |
| `bus:delivery` | `EpisodeDelivered`, `ContinuityUpdated` |
| `bus:system` | `BudgetExhausted`, `NodeUp{node}`, `NodeDown{node}`, `Heartbeat` |
| `bus:dead` | poison events (DLQ) |

**Event envelope (every event):**
```json
{
  "event_id": "uuid",          // global dedup key (idempotency)
  "type": "ShotScheduled",
  "ts": "2026-08-07T02:00:01Z",
  "run_id": "run-2026-08-07",
  "show_id": "neon-sutra",     // multi-show: every event is scoped to a show (§8/§17)
  "episode": 12,               // per-show episode number
  "source_node": "renderer-A",
  "correlation_id": "s01_sh01",
  "payload": { "...": "..." }   // small inline data + artifact refs (paths on the SMB share)
}
```

**Multi-show:** streams are **shared**, with `show_id` in the envelope; consumers filter by `show_id` and per-show streams are not needed. `RunRequested` carries the target `show_id`. The BudgetGate allocates the nightly window across the shows enabled for that night (§17).

**Delivery semantics:**
- **At-least-once**, one consumer group per stage; handlers are **idempotent** (check `event_id` in the SQLite `events` table; already-seen → ack and skip).
- **Ordering:** shots are independent (no global order needed). Linear stages (dev → script → keyframes) use a single consumer so their ordering is trivially serial.
- **Retry:** failed handling → N retries with backoff, then move to `bus:dead` with the traceback; a DLQ watcher surfaces it in the run report (never silently dropped).
- **Backpressure:** a `BudgetGate` consumer tracks `RunProgress` and emits `BudgetExhausted`; producers stop emitting `ShotScheduled`/`RetakeRequested` until the next `RunRequested` (see §15.2).
- **Resume after crash:** consumer groups resume from the last acknowledged event; SQLite stage state reconciles to the same point (§16.3).
- **Node failure:** renderer A's agent gets a health-check on `bus:system`; if A is down, `ShotScheduled` events stay pending in the stream (delivered when A returns) — no orchestration needed.

**Broker on Windows:** native Redis for Windows (Memurai, or a Redis binary via Docker Desktop/WSL). Config `bus.url`, `bus.password`, `bus.group_prefix`. If a broker dependency is unwanted, `bus.provider: none` falls back to in-process queue + DB polling (the old §17 model) — but the event choreography and schemas are identical either way.

---

## 7. Toolchain contracts

### 7.1 ComfyUI (shared by image + video)

- Local server at `http://127.0.0.1:8188`. In two-node mode (§15.5) there are two instances, both loopback-only:
  - **Renderer node A:** H3 ComfyUI bound to `127.0.0.1:8188`, driven by the **renderer agent** (`studio.py renderer`), which is the only process that talks to it. No LAN listener needed.
  - **Worker node B:** image (Krea 2) ComfyUI bound to `127.0.0.1:8188`.
- The **only** LAN control-plane service is the event-bus broker on node B (Redis :6379); A's agent connects out to it. See §6.4, §15.5.
- API used: `POST /prompt` (submit graph), `GET /queue`, `GET /history`, WebSocket `ws://127.0.0.1:8188/ws` (progress, status, executed nodes), `POST /upload/image` (inputs). Optionally enable ComfyUI's API key even on loopback.
- Both workflows are **templates** (JSON) parameterized by the orchestrator; no canvas editing in prod.
- ComfyUI version **≥ 0.30.0** required (H3 support + `comfy_api.latest` + packed AV latent).
- Two checkpoints for H3 (not interchangeable): `fl2va` (t2v + first/last keyframes, "Refs OFF") and `ref2va` (reference images/audio/video, "Refs ON"). The Director node's toolbar switch selects which loads; only the selected one reads from disk. See §10.3.

### 7.2 LM Studio (Showrunner LLM + judge + prompt enhancer)

- OpenAI-compatible server at `http://127.0.0.1:1234`.
- Endpoints used: `POST /v1/chat/completions` with `response_format={"type":"json_object"}` for structured stage outputs.
- Models are config per role (see §11.2). All inference is local.
- VRAM sharing with ComfyUI is managed by the orchestrator (§15.4): creative stages run while the GPU is otherwise idle; before H3 renders, LM Studio is told to reduce offload if configured.

### 7.3 TTS engines

- Any OpenAI-compatible / local TTS or engine with a CLI (e.g., Piper, Coqui XTTS, Edge-TTS bridge, ElevenLabs *local* never used — hosted TTS is banned by NFR-3, so only local engines). The user says "we have many TTS options" — the **VoiceActor registry** (§12) abstracts engine choice per character.
- Each engine exposes: `synthesize(text, voice_id, speed, pitch, out_wav) -> duration`.

### 7.4 ffmpeg

- Assembly: concat demuxer, crossfade, loudnorm, mux, encode H.264 (crf 20, preset medium) or H.265 for archive.
- QC probes: `ffprobe` for duration/frame count/streams; `ffmpeg -vstats` or signal analysis for grey/black frame detection (NaN/VAE-failure symptom).

### 7.5 SQLite

- Single DB `studio.db` with tables: `runs`, `stages`, `shots`, `assets`, `continuity`, `characters`, `voices`, `policy_events`, `retakes`, `qc_results`.

### 7.6 Event source (was "Scheduler")

- The **event source** (see §17): a timer process on node B publishes `RunRequested` nightly (e.g., 02:00). A Windows Task Scheduler task may be used only as the alarm that starts `studio.py serve`; it never drives the pipeline itself.
- Single-instance lock file prevents overlapping runs.

### 7.7 Portability & provisioning (per-node, self-contained)

**Principle (user requirement): everything is portable and per-machine.** No system-wide installs, no shared CUDA/python environments between use cases. Each node is self-contained; the studio only talks to its local instances over loopback (+ one LAN broker on B). This is the same pattern as the user's LanguageLearner portable ComfyUI trees.

- **Portable LM Studio** — lives under the user's `~/.lmstudio` (or a `portable/lmstudio/` dir); models are local files, managed via the `lms` CLI (`lms server start --port 1234`, `lms load <model> --gpu max -c <ctx>`, `lms unload --all`). The pipeline drives it through `lms` + health-polls the OpenAI-compatible API. **Node B runs the LLM roles (showrunner/judge/reviewers/describer); node A needs no LLM.** `studio.py setup` verifies `lms` + the configured model files.
- **Portable ComfyUI, one per use case** — two self-contained trees (each with its own `python_embeded`, `custom_nodes/`, `models/`, and pinned CUDA build), so node/CUDA/version conflicts never leak between projects or between the image and video roles:
  - `node B → krea2/` (IP-Adapter, ResolutionSelector; bind `127.0.0.1:8188`)
  - `node A → h3/` (MiniMaxH3-Director, VHS; bind `127.0.0.1:8188`, driven by the renderer agent)
  - The GPU manager's `COMFYUI` load/unload starts/stops the portable tree's run script (e.g. `run_nvidia_gpu.bat`) and waits on `/system_stats` (§15.4).
- **Portable studio venv** — the pipeline code runs from a per-project `.venv` (PyYAML, httpx, optional redis); `studio.py setup --venv` creates it and `pip install -e .`.
- **Portable ffmpeg/ffprobe** — `config/env.yaml → ffmpeg` points at a portable `ffmpeg.exe` (or PATH); the client resolves it once.
- **Portable Redis (optional, multi-node)** — Memurai or a portable redis binary on node B; single-node dev can stay on the in-memory broker (`bus.provider: memory`).
- **Machine role** — `config/env.yaml` declares this machine's `node: worker | renderer`; `serve` (B) and `renderer` (A) refuse to start the wrong role's services and only manage that role's portable instances.
- **`studio.py setup`** — per-machine provisioning checklist: python/venv/deps, ffmpeg, `lms` + models, the role's portable ComfyUI tree, broker reachability. Idempotent; never modifies other projects.

### 7.7a Remote control plane (drive every node from the controller)

Everything can be run from **one controller machine** (this repo's primary box):

- **Remote agent (`studio.py remote-agent`)** — a small token-gated HTTP service running on each node (default `0.0.0.0:8123`). It executes local service ops only: LM Studio (`lms server start/load/unload/status/get` + full `provision`), portable ComfyUI start/stop, `verify`. It is the *service-ops* control plane, distinct from the event bus (which stays the *pipeline* control plane).
- **Controller CLI (`--remote <role|host[:port]>`)** — `studio.py setup --lmstudio --remote worker`, `studio.py lms load|unload|status --remote …`, `studio.py comfy start|stop --remote …`, `studio.py doctor --remote …`. Endpoints come from `config/remotes.yaml` (roles `worker`/`renderer`) or a raw `host:port`.
- **`setup --lmstudio`** — one command that on the target node: finds `lms`, starts the server, ensures the configured model is on disk (`lms get` if missing), loads it (`--gpu <ratio>` — `env.yaml lmstudio.gpu_ratio`, e.g. `0.5` for the 1660 Super's 6 GB), health-polls the OpenAI API, and writes `env.yaml`. `lms load --estimate-only` tunes the ratio first.
- The GPU manager uses the same `gpu_ratio` when it loads the LLM during a run, so a partially-offloaded model on the worker behaves the same whether it was loaded by hand or by the controller.

### 7.7b Deployment field notes (proven on Beast3, home workgroup)

Live-learned during first remote provisioning; codified so the next node is faster:

- **WinRM over a home network (workgroup)** needs, on the target: `Enable-PSRemoting -Force`; a firewall rule for 5985 (the auto-rule fails when the network profile is **Public** — `Set-NetConnectionProfile -NetworkCategory Private` or `New-NetFirewallRule -LocalPort 5985`); and **`LocalAccountTokenFilterPolicy=1`** (otherwise valid local-admin credentials still yield "Access is denied"). A **non-blank password is mandatory** (Windows blocks blank-password network logon). The client must add the target to its own `TrustedHosts` (`Set-Item WSMan:\localhost\Client\TrustedHosts ...`).
- **Credentials without chat leakage:** `(Get-Credential "BEAST3\chad") | Export-Clixml "$env:TEMP\beast3-cred.xml"` — DPAPI-scoped to the same Windows user, so the controller shell can `Import-Clixml` it. Inside `Invoke-Command` scriptblocks `$cred` is **null** (closures don't serialize) — pass it via `-ArgumentList` + `param($c)`.
- **WinRM-spawned daemons die with the session** (session-0 teardown). Services/daemons must run under a **scheduled task at boot** (`schtasks /Create /SC ONSTART /RU <user> /RP <pw> /RL HIGHEST`) or a Windows service, not from an interactive WinRM call. `New-Service -Credential` does **not** grant "Log on as a service" (secedit required) — the scheduled task avoids that.
- **Portable LM Studio inside the project folder:** install with `Setup.exe /S /D=C:\<root>\portable\lmstudio`. Its data dir defaults to `%USERPROFILE%\.lmstudio`; to keep it self-contained, run it once, then move that folder into `portable\lmstudio\data` and put a **directory junction** back at `%USERPROFILE%\.lmstudio`. `lms` lives at `...\resources\app\.webpack\lms.exe`; point `env.yaml lmstudio.cli` at it and `gpu_ratio: 0.5` on 6 GB cards.
- **Portable runtime stack:** `uv` + `UV_PYTHON_INSTALL_DIR/UV_CACHE_DIR` inside `portable\uv`, venv at `<root>\.venv`; portable `ffmpeg` at `portable\ffmpeg` (`env.yaml ffmpeg`). Everything resolves under `<root>` (single project folder, no profile scattering).
- **`config/remotes.yaml` must be FLAT** (controller/worker/renderer at top level) — a nested `remotes:` key collides with the config section name and silently falls back to `127.0.0.1`.
- **CUDA driver floor:** portable ComfyUI trees ship a fixed torch (`2.12+cu130` in the LanguageLearner Krea2 tree), which needs an NVIDIA driver **≥ 580.x** for CUDA 13. Symptom: `cudaGetDeviceCount() → cudaErrorNotSupported` at ComfyUI startup on machines with an older driver (e.g. 560.94/CUDA 12.6). Fix: update the GeForce driver on the node (1660 Ti → 610.88 here) + reboot. Diagnose with `nvidia-smi` and `python_embeded\python.exe -c "import torch; print(torch.cuda.is_available())"`.
- **Agent-spawned daemons persist; WinRM/manual ones don't.** ComfyUI/LM Studio started via `studio.py comfy start --remote` / `setup --lmstudio --remote` (the scheduled-task agent) survive across sessions and reboots; anything started from an interactive `Invoke-Command` dies when that session closes. Always drive services through the agent on a node.

---

## 8. Canonical data model

All files are JSON/YAML; schemas are pinned in `schema/` at build time.

**Multi-show layout:** the system runs **one or more shows**, each a self-contained universe under `shows/<show_id>/`. A show owns its bible, cast, voices, scenes, continuity state, episode numbering, and all its artifacts. Show IDs are slugs (`neon-sutra`). Every bus event and DB row is `show_id`-scoped (§6.4, §17).

```
shows/<show_id>/
├── bible.yaml
├── characters/<id>.yaml …        # + refs/ per character (ref bank)
├── voices/<id>.yaml …
├── scenes/<location>.yaml …
├── continuity/state.json         # per-show; episode counter lives here
├── runs/EP##/…                   # plans, scripts, reviews, reports (per-show numbering)
├── assets/EP##/…                 # keyframes, shots, dialogue
└── archive/EP##/                 # delivered episodes + feed.json
```

### 8.1 `shows/<show_id>/bible.yaml`
> **AI-generated, not hand-authored.** Produced by the Showrunner at `init-show --generate/--brief` (Gate 0, §9.5) and approved by the user. The user may edit it later (or add characters/themes at runtime, §9.5a), but editing is an override, never a requirement. Same for `characters/`, `voices/`, and `scenes/` below.
```yaml
series:
  title: "Neon Sutra"
  logline: "..."
  genre: [cyberpunk, romance]
  tone: ["noir", "earnest", "melancholic"]
  runtime_target_s: 1320            # 22:00
  aspect_ratio: "16:9"               # H3 valid set: 21:9,16:9,4:3,1:1,3:4,9:16
  resolution: [1344, 768]            # 16:9 native (768 short edge)
  fps: 24
  language: "en"                     # English only (dialogue, narration, subs config) — §12
  style_guide: "line-art over painted bg; ..."   # THE style lever — injected into every image prompt.
                                              # Krea 2 renders cel / 3D / 2.5D / 80s / 90s anime looks;
                                              # this string (AI-proposed at Gate 0, user-approved) picks the show's look.
  quality_baseline: "ranma-1-2"      # benchmark reference: 1990s martial-arts romcom anime — the show's
                                              # production-quality and tonal bar (used in reviewer rubrics, §9 Stage 2a)
world:
  setting: "..."
  rules: ["...", "magic system..."]
  established_facts: ["..."]          # mutable; canonical copy lives in continuity state
arcs:
  - id: arc_1
    name: "Rise of the Courier"
    beats_total: 6
    beats:
      - id: beat_1
        summary: "..."
      # beats are the unit of Development; an episode realizes 1+ beats
episodes_per_arc: 6
content_policy: "mature"              # see §13; legal values: clean | mature
# mature_spec — AI-generated at Gate 0 with the bible, user-approved. The fan_service reviewer (§9 Stage 2a)
# enforces this as the show's creative floor (the inverse of the policy judge).
mature_spec:
  quotient: "explicit_ecchi"          # light_ecchi | ecchi | explicit_ecchi — the show's promised maturity level
  quotas:
    service_scenes_per_episode: [1, 2]     # min..max — the fan_service reviewer checks the episode hits this
    reward_scenes_per_arc: 3                # special scenes (reward-style, §LanguageLearner precedent)
  escalation:
    schedule: "arc_position"                # intensity grows with arc position; season opener ≠ finale
  tone_boundaries: ["consensual", "adult-fictional-only", "no-degradation"]
  characters:                               # which characters carry service scenes, and their comfort roles
    kiyo: "primary"
    rei: "support"
  scene_types: ["tease", "reward", "intimate"]   # catalog the script may draw from
```

### 8.2 `shows/<show_id>/characters/<id>.yaml`
```yaml
id: kiyo
name: "Kiyo"
role: protagonist
appearance_canonical: "mid-20s woman, silver bob cut, cybernetic left arm, ..."  # injected into EVERY image/video prompt (never varies)
appearance_notes: "never change eye color; scar on right cheek; uniform variant A/B/C"
personality: ["terse", "wry", "loyal"]
traits_for_llm: "speaks in short sentences; avoids direct answers; calls partner 'corpo'"
voice: kiyo_jp                 # → VoiceActor id (§8.3)
h3_slot: "@char1"             # which Director character slot this binds
ref_images: []                # ref bank: Krea 2 seed sheet + QC-verified in-show stills (self-reinforcing, §14.6); ≤ 9 (H3 limit)
outfit_state: {}              # per-episode costume decision record (see §14.5): which variant is canonical right now
# Krea 2 canonical reference images live in shows/<show_id>/characters/<id>/refs/
#   Generated ONCE at bootstrap under "reference-sheet discipline": neutral pose, plain/gray background,
#   minimal accessories — deliberately staged to make IP-Adapter / ref2va conditioning strong (§14.5).
```

### 8.3 `shows/<show_id>/voices/<id>.yaml`
```yaml
id: kiyo_jp
engine: qwen3_tts             # concrete: Qwen3-TTS 12Hz 1.7B (CustomVoice + VoiceDesign) — see §12.1
mode: designed                # "preset" → fixed speaker from Qwen3-TTS pool; "designed" → unique voice from text
speaker: null                 # mode=preset: one of [Aiden, Dylan, Eric, Ono_anna, Ryan, Serena, Sohee, Uncle_fu, Vivian]
voice_description: "low, husky, sardonic woman in her 20s, subtle electronic reverb, clipped delivery"
voice_fingerprint: ""         # sha256(speaker|voice_description) — the cache key; changing it = a NEW voice
speed: 1.0
pitch: 0.0
h3_audio_role: reference      # how dialogue feeds H3 for this character
```

### 8.4 `shows/<show_id>/continuity/state.json`
```json
{
  "episode": 0,
  "world": {"kaiju": "active", "north_district": "quarantined"},
  "characters": {"kiyo": {"status": "alive", "owed_debt": true, "arc_position": 1}},
  "plotlines": [
    {"id": "debt_plot", "status": "open", "last_seen_episode": 0, "notes": "creditor is Rei"}
  ],
  "unresolved_threads": ["who runs the black market?"],
  "continuity_rules": ["Never contradict: Kiyo lost her left arm before the series started"]
}
```
Updated by the Showrunner after each delivered episode (**continuity_delta**).

### 8.5 `runs/EP##/plan.json` (Stage 1 output)
```json
{
  "episode": 12,
  "arc": "arc_1",
  "beats_covered": ["beat_3"],
  "synopsis": "...",
  "cold_open": "...",
  "scenes_planned": 14,
  "estimated_shots": 138,
  "continuity_notes": ["payoff: Rei reveals the debt was a test"],
  "status": "planned"
}
```

### 8.6 `runs/EP##/script.json` (Stage 2 output)
```json
{
  "episode": 12,
  "scenes": [
    {
      "id": "s01",
      "location": "north_district_rooftop",
      "time_of_day": "night",
      "summary": "Kiyo gets the package",
      "shots": [
        {
          "id": "s01_sh01",
          "type": "ref2va",            // ref2va if it uses references, else fl2va
          "importance": "hero",        // hero → best-of-2 + extra QC; standard → 1 seed
          "duration_s": 10.125,         // grid-snapped (k=14 -> 243f -> 10.125s)
          "camera": "wide establishing, slow push-in",
          "action": "Kiyo lands on the rooftop, looks over the district",
          "dialogue": [
            {"char": "kiyo", "line": "The package. Where is it?", "start_s": 1.0, "end_s": 3.4, "on_camera": true}
          ],
          "soundscape": "wind, distant sirens",
          "music": "bass drone",
          "references": {"characters": ["kiyo"], "scene": "north_district_rooftop", "style": "series_style"},
          "keyframes": {"first": "s01_sh01_a", "last": "s01_sh01_b"},  // prompt ids for Stage 3
          "maturity_flags": ["mild_violence"]
        }
      ]
    }
  ],
  "status": "scripted"
}
```

### 8.7 `runs/EP##/shot_list.json` (Stage 3 output)
Adds per-shot: `keyframe_prompts` (for Krea 2), `h3_prompt` (the compiled H3-notation storyboard text, §10.4), `audio_plan` (dialogue file refs + mode), `importance` (inherited from script; `hero`/`standard`), `candidate_seeds` (`[s1]` for standard, `[s1, s2]` for hero best-of-2), `status: "pending"`, and a `qc` slot filled in at Stage 8 with the Layer 0–2 results.

### 8.8 `studio.db` — core tables (DRAFT)
```sql
CREATE TABLE runs(
  id INTEGER PRIMARY KEY, show_id TEXT NOT NULL, episode INT NOT NULL, status TEXT,
  started_at TEXT, finished_at TEXT, operating_mode TEXT, nightly_budget_min INT,
  progress_json TEXT, UNIQUE(show_id, episode));
CREATE TABLE stages(
  id INTEGER PRIMARY KEY, run_id INT REFERENCES runs(id), stage INT, name TEXT,
  status TEXT, started_at TEXT, finished_at TEXT, input_hash TEXT, output_path TEXT);
CREATE TABLE shots(
  id TEXT PRIMARY KEY, run_id INT, shot_idx INT, scene_id TEXT, status TEXT,
  duration_s REAL, grid_frames INT, attempts INT, video_asset_id TEXT, audio_asset_id TEXT,
  seed INT, prompt_hash TEXT, qc_json TEXT);
CREATE TABLE assets(
  id TEXT PRIMARY KEY, run_id INT, kind TEXT, stage INT, path TEXT, prompt_hash TEXT,
  seed INT, checksum TEXT, status TEXT, created_at TEXT);
CREATE TABLE retakes(
  id TEXT PRIMARY KEY, shot_id TEXT, reason TEXT, attempt INT, budget_hit INT, status TEXT);
CREATE TABLE continuity(show_id TEXT PRIMARY KEY, state_json TEXT, updated_at TEXT);
CREATE TABLE qc_results(id TEXT PRIMARY KEY, shot_id TEXT, checks_json TEXT, composite REAL,
  verdict TEXT, passed INT, created_at TEXT);
CREATE TABLE script_reviews(id TEXT PRIMARY KEY, run_id INT, episode INT, round INT,
  reviewer TEXT, script_version TEXT, status TEXT, score REAL, notes_json TEXT, created_at TEXT,
  UNIQUE(episode, round, reviewer));
CREATE TABLE policy_events(id INTEGER PRIMARY KEY, run_id INT, stage INT, artifact_hash TEXT,
  verdict TEXT, reason TEXT, created_at TEXT);
```

---

## 9. Episode pipeline — stage-by-stage spec

Every stage is an **event consumer**: it receives its trigger event, reads state, runs, **commits** (transaction + asset registry entries), emits the next event, and acks. A crash before ack = the event is redelivered and the stage re-runs idempotently (dedup on `event_id`). "The orchestrator" below = the stage consumers + the run controller running under `studio.py serve` (one process per node, each hosting the consumers for its node's departments, §15.5).

### Stage 0 — Run bootstrap (event consumer) (event consumer)
- **Trigger:** a `RunRequested{episode, mode, budget_min}` event published by the timer source (in-process APScheduler in `studio.py serve`, or the optional Task Scheduler alarm). Manual trigger: `python studio.py request-run`.
- Consumed by the run controller on node B. Acquires `studio.lock` (broker + DB); a duplicate `RunRequested` with the same `event_id` is deduped.
- Opens DB (WAL), computes budget; sets up the run consumer groups if missing.
- Determines current episode number = `max(continuity.episode) + 1` unless a run is already in-flight (resume path).
- If a previous run for EP## is incomplete, **resume** it (§16.3) rather than starting a new plan; emits `RunStarted`, then `RunProgress` periodically.
- On `BudgetExhausted` (from the BudgetGate) → emit `RunAborted{paused: true}` so the next night's `RunRequested` resumes cleanly.

### Stage 1 — Development (Showrunner)
- **Inputs:** bible, continuity state, arc beats, episode number, `in_progress_continuity`.
- **Process:** LLM produces `plan.json`: synopsis, beats covered, continuity notes, estimated scene/shots, hook for next episode. Runs at temp 0.8, long context.
- **Guarantee:** never contradicts `continuity.state`; the system prompt injects `continuity_rules` verbatim.
- **Failure:** malformed JSON → 2 retries with repair prompt → if still bad, abort run (no generation wasted).

### Stage 2 — Script (Showrunner)
- **Inputs:** `plan.json`, character sheets (personality/traits), continuity.
- **Process:** emits `script.json` with scenes, locations (drawn from a **scene registry** — see Stage 3), shots, camera language, dialogue lines. Dialogue lines are written with **target durations** in mind (`end_s - start_s` matches TTS + expected delivery pace).
- **Constraints encoded in the LLM prompt:**
  - Total runtime within target; shots must map to valid H3 grid durations (§10.2).
  - ≤ 9 reference images, ≤ 3 ref videos, ≤ 3 ref audio per shot (H3 hard limits).
  - Fictional characters only; content policy applied by the judge after this stage.
  - **Episode costume/setting state (§14.5):** the Showrunner carries the current `outfit_state` and applies the "keep or change outfit?" decision per scene (change only on location change / time-skip / story beat); the resulting state is written to `character.outfit_state` and continuity, so outfits stay stable *and* deliberate.
- **Output:** `script.json` (this is *Draft r1* — versioned from here: `script.r1.json`, `script.r2.json`, …).

### Stage 2a — Script review & revision (the writers' room)

After the Showrunner drafts the script, it goes through a **structured revision loop mirroring real script processes** — draft → notes → revised draft → re-notes — before the human Story gate. Multiple **specialized reviewers** run in parallel, and **each keeps its own separate notes file**, exactly like a Script Supervisor's notes vs an Editor's notes in a real production.

**Reviewer roles** (`config/reviewers.yaml`; extensible list, defaults below):

| Reviewer | What it checks | Notes file | Output |
|---|---|---|---|
| **`slop`** (AI-slop detector) | AI-writing tells per scene & line: repetitive sentence patterns, filler/weasel words, on-the-nose dialogue, exposition dumps, predictable beat shapes, cliché emotional beats, purple prose, generic safe choices. Its rubric borrows from the `stop-slop` skill's anti-AI-tell principles | `runs/EP##/reviews/slop.r{N}.json` | per-scene/line slop score + notes + rewrite directives |
| **`continuity`** | Cross-checks every scene/shot/dialogue against the show's `continuity/state.json`, character sheets (`personality`, `traits_for_llm`, `appearance_notes`, `outfit_state`), the scene registry, the episode timeline, and `continuity_rules` — contradictions, character-voice drift, timeline violations, forgotten unresolved threads, set/prop/wardrobe continuity | `runs/EP##/reviews/continuity.r{N}.json` | per-item continuity notes with severity |
| **`fan_service`** (maturity delivery) | Enforces the **creative floor** the show promised: does the episode deliver the mature quotient from the approved `mature_spec` (§8.1) — per-episode quota of service/reward scenes, correct characters carrying them, motivated + well-paced escalation, no bait-and-switch (an episode that was promised mature but goes clean, or under-delivers relative to arc position), tone inside the approved boundaries. This is the *inverse* of the policy judge: `judge` blocks what must never appear; `fan_service` flags when the intended mature content **isn't there or lands flat**. Uses the reward-scene segment catalog precedent from LanguageLearner | `runs/EP##/reviews/fanservice.r{N}.json` | per-scene maturity score vs `mature_spec` quota + notes + rewrite directives |

Notes use the standard screenplay-notes form: `[Scene s01] Kiyo says "X" — note.` Each note is attributed to its reviewer and stays in that reviewer's file; the merged set is only ever *projected* for the Showrunner to apply.

**The revision loop (events on `bus:script`):**

```
ScriptReady (r1)
  → ScriptReviewRequested{round:1}
  → ScriptNotesReady{reviewer: slop, round:1}        ┐ (parallel LLM calls,
  → ScriptNotesReady{reviewer: continuity, round:1}  │  separate notes files)
  → ScriptNotesReady{reviewer: fan_service, round:1} ┘
  → all pass  ⇒ ScriptReady (final) → Gate 3 (Story)
  → notes exist ⇒ ScriptRevisionRequested{round:1}
      → Showrunner revises (applies ALL notes, each attributed) ⇒ script.r2.json → ScriptRevised{round:2}
      → loop: re-review r2 …
  → exit: all pass, OR max_revisions reached ⇒ ScriptReady(final) with residual notes flagged
```

- **Fixed-point loop:** reviewers re-check the newest revision only; notes that clear are recorded as resolved in that reviewer's file (round history preserved). Capped at `max_revisions` (default 2) so a stubborn scene can't loop forever.
- **Exit to human:** regardless of pass/fail, the reviewer notes travel with the script to the Story gate (Gate 3) — the human sees **all** reviewers' separate notes side by side, the revision history, and can approve, reject-with-notes (which starts one more revision round), or add their own notes.
- **Cost:** text-only LLM calls on node B under `gpu_manager.acquire(LLM)` — cheap, runs before any GPU-heavy keyframe/H3 spend. Reviewer models are small (see §11.2).
- **Output:** `script.rN.json` + `runs/EP##/reviews/{slop,continuity,fanservice}.rN.json` + `script_reviews` rows.

**Extends beyond the script:** the `fan_service` reviewer's rubric is re-applied at two later checkpoints so the intended mature content isn't lost in translation from text to pixels:
- **Stage 3 (keyframes):** service scenes' shot descriptions are validated against the approved framing/intensity targets (the visual brief must deliver the beat).
- **Stage 8 (QC):** shots from service scenes carry a `fan_service` axis in the Layer-1 rubric (delivery of the intended framing/intensity), and the whole-cut judge re-checks the episode-level quota.

### Stage 3 — Storyboard, keyframes & scene registry (Krea 2)
- **Scene registry:** `shows/<show_id>/scenes/<location>.yaml` with a canonical description and a **scene reference image** (generated once by Krea 2 and cached). The LLM reuses existing scenes; new locations are proposed to Krea 2.
- **Per-shot keyframes:** for each shot, Krea 2 generates `first` and `last` frame stills from `keyframe_prompts` (appearance_canonical + outfit_state + location + camera + action + style_guide). 16:9, matching H3 output resolution (1344×768). Shots that feature a character use **`image_keyframe_ipadapter.json`** (IP-Adapter with that character's ref, §10.1/§14.5); background/establishing shots use plain `image_keyframe.json`.
- **Character ref bank:** at bootstrap (first run), Krea 2 generates 3–6 character reference images per character under **reference-sheet discipline** (face close-up, full-body front, full-body side; neutral pose, plain background, minimal accessories). These refs feed **both** consumers — IP-Adapter on keyframes and H3 `ref2va` — and are cached forever (regenerated only when the user edits the character sheet). See §14.5.
- **Shot prompt compilation:** orchestrator compiles `h3_prompt` from the shot data using the MiniMax notation (§10.4). No LLM needed here — it is deterministic string-building, which keeps the "exact prompt the model receives" reproducible.
- **Output:** `shot_list.json` + `assets/EP##/keyframes/*.png` + registry entries.
- **Failure:** Krea 2 job error → 2 retries (seed change) → mark shot `keyframes_failed`; abort episode (do not spend H3 on a shot without keyframes).

### Stage 4 — Pre-render validation (deterministic)
Pure validator, no LLM. Checks every shot:
- `duration_s` is on the 17k+5 grid and within [4,15] s (§10.2). Snap otherwise.
- Reference counts within H3 limits; style/character refs resolved to actual files.
- Keyframe images exist, aspect matches, dimensions ≤ 1344×768.
- Dialogue lines ≤ shot duration with ≥ 0.4 s head/tail slack.
- Audio ref count ≤ 3 and each ≤ 15 s.
- Content-policy scan (judge LLM) passed for every prompt artifact (logs to `policy_events`).
- Seed recorded per shot.
- **Output:** `shot_list.validated.json`; violations → auto-fix (snap duration) or hard-fail with report.

### Stage 5 — Shot production (MiniMax H3 via ComfyUI Director)
- **Runs on the renderer node A** (renderer agent): consumes `ShotScheduled{shot_id}` events, builds the H3 workflow JSON (§10.3) and submits via `POST /prompt`.
  - **fl2va path** (no character references, t2v + keyframes): text prompt + first/last keyframes.
  - **ref2va path** (character references): character refs via `@charN` slots → `<Subject N>`, scene/style refs via `ref_images`, audio refs via the Audio track (the `DialogueReady` asset from Stage 6).
- Watch progress over the WebSocket; poll `GET /history` for completion; pull the output file from `ComfyUI/output/`; emit `ShotRendered{asset_ref}` (or `ShotFailed{reason}`).
- **Retake on QC-fail:** a `ShotQcFailed` event → `RetakeRequested` → re-submitted via H3 **Retake Mode** (re-render the range anchored on the base video's own frames either side, first/last = base frames) and stitched with **MiniMax H3 Retake Stitch** (§10.5).
- **Per-shot budget:** `max_attempts = 2` renders, then emit `ShotSkipped` and continue (episode completes with missing shots flagged — never silently).
- **Output:** `assets/EP##/shots/<id>.mp4` (+ joint audio in the same file).

### Stage 6 — Dialogue production (TTS)
- Consumes `ShotScheduled`/`ScriptReady`; for each shot with dialogue: resolve each line's VoiceActor, synthesize to `assets/EP##/dialogue/<id>__<char>.wav`.
- Concatenate + pad into a single **shot dialogue track** timed to the shot (silence pads between lines).
- `audio_plan.mode` selects how it reaches the episode:
  - `h3_reference` — dialogue clip is uploaded and dropped on the Director Audio track as `<Audio j>`; H3 muxes it with generated soundscape **and conditions the video on it, giving in-model lip-sync**. This is the preferred path whenever a character is on camera speaking. (Only when ≤ 3 audio refs and each ≤ 15 s.)
  - `ffmpeg_mux` — H3 runs on ambience only; dialogue is mixed over the shot in Assembly (§7). Guaranteed-correct path.
  - `h3_only` — no dialogue in this shot.
- The orchestrator **prefers `h3_reference` whenever a shot has `on_camera` dialogue** (it buys in-model lip-sync) subject to the ≤3-audio-ref / ≤15-s limits; `ffmpeg_mux` is the reliability fallback for off-camera lines, oversized dialogue, or QC degradation (§12.3).
- **Output:** per-shot dialogue tracks + registry; emits `DialogueReady{shot_id, asset_ref}` so the renderer can include the audio ref on its next render pass.

### Stage 7 — Assembly (ffmpeg)
- Order shots by `shot_idx`; join with 1-frame crossfades (optional, config).
- Sequence: cold open → title card (OP card) → scenes → ED card.
- Audio: concat each shot's muxed track; where `ffmpeg_mux`, overlay TTS dialogue at recorded offsets with `loudnorm`; add OP/ED stingers if provided.
- Encode: H.264 CRF 20 24fps (preview) and H.265 (archive, optional).
- **Output:** `runs/EP##/out/episode_preview.mp4`.

### Stage 8 — QC & retakes

Consumes `ShotRendered` and `AssemblyReady`. **Quality is scored in four layers.** Layers 0–2 are automatic; layer 3 is human. "Good" = passes the hard gates (L0/L1) and scores above threshold on the rubric (L1/L2). Because H3 returns **one render per prompt** (no candidate set), QC is not just a filter — its scores *drive retakes and best-of-N*.

**Layer 0 — Deterministic signal probes (no AI, milliseconds).** Catches the "broken render" class:

| Check | Tool | Fail → action |
|---|---|---|
| Duration == grid target (±1 frame) | ffprobe | `ShotQcFailed` → auto-retake |
| No grey/black/NaN frames (VAE failure) | ffmpeg `signalstats` | `ShotQcFailed` → auto-retake |
| No frozen/stutter frames (consecutive identical frames above threshold) | ffmpeg `framemd5` / OpenCV frame-diff | `ShotQcFailed` → auto-retake |
| Audio present, non-silent | ffprobe / ebur128 | `ShotQcFailed` → auto-retake |
| **Anchor adherence** — SSIM between rendered first/last frames and the input keyframes. H3 is *supposed* to anchor on them; a low SSIM means it ignored our framing | OpenCV SSIM | `ShotQcFailed` → auto-retake with seed change |
| **Boundary continuity** — SSIM between shot N's last frame and shot N+1's first frame. Big drop = visible jump cut | OpenCV SSIM | flag for review / re-order |
| Reference-count + policy re-scan | validator | hard-fail |
| Shot sequence order | path audit | fix |

**Layer 1 — Vision-judge rubric (local vision LLM via LM Studio `describer`, sampled frames).** For each shot, sample 3–5 frames (start, 25%, 50%, 75%, end) plus dialogue-peak frames; the judge returns structured JSON:

```json
{
  "prompt_adherence": {"score": 0.0, "notes": "subject present, wrong action"},
  "identity":        {"score": 0.0, "ref_match": true, "notes": "face matches ref bank"},
  "lip_sync":        {"score": 0.0, "notes": "mouth moving at dialogue peak"},
  "artifacts":       {"score": 0.0, "issues": ["warped hand"]},
  "style":           {"score": 0.0, "notes": "matches style guide"},
  "fan_service":     {"score": 0.0, "notes": "delivers intended framing/intensity"},  // service-scene shots only (§9 Stage 2a)
  "composite":       {"score": 0.0}
}
```

- The identity axis is checked **against the character ref bank** (the judge is shown the shot frames *and* the character's ref image). Frames that pass identity *and* are crisp become **candidates for ref-bank promotion** (§14.6), so good renders improve future renders.
- Hard-fail axes: `identity < 0.4` or `prompt_adherence < 0.4` → `ShotQcFailed` → auto-retake.
- Content-policy re-scan rides along on the same call (verdict + reason → `policy_events`; hard block).
- Sample frequency is throttled so judging ~140 shots costs minutes, not hours (see §15.2 budget).

**Layer 2 — Cross-shot continuity judge (after assembly, phase 2).** A judge pass over sampled frames across the whole episode, given the continuity state + scene registry, catches drift that per-shot judging can't: outfit changed mid-scene, location mismatch, tone/color shift, timeline contradiction. Outputs flagged frames for human review.

**Layer 3 — Human spot-check (dashboard, M5).** Thumbnail contact sheet per shot + per-episode; approve/reject. Production runs unattended; "airing" can be gated on approval. Every layer's data is visible in the dashboard so a human can see *why* a shot was retaken.

**Retake & best-of policy (score-driven):**
- Hard-fail (L0, or L1 hard axis) → `ShotQcFailed → RetakeRequested → ShotScheduled`, new seed, up to `max_retakes_per_shot` (default 2), gated by the BudgetGate.
- **Best-of-N for hero shots:** the script tags `importance: hero` on money shots (~10–20% of an episode). Hero shots render with **2 seeds**; both are L1-judged; the higher-composite candidate is kept. Standard shots render 1 seed.
- **Budget:** BudgetGate counts renders + retakes against the nightly budget; on `BudgetExhausted`, unrendered shots stay `ShotScheduled` and complete on the next `RunRequested`.
- A **QC report** (`runs/EP##/qc.json`, emitted as `QcReportReady`) records every layer for every shot and the whole cut — always produced, even on full pass.

**Scoring calibration (M1 exit gate):** thresholds (`anchor_ssim_min`, `composite_min`, `identity_min`, `artifact_retake`) live in `config/qc.yaml` and are calibrated empirically at M1 against ~20 real renders, not guessed. The pipeline ships with conservative defaults and the M1 milestone tunes them.

- **Output:** `runs/EP##/out/episode_final.mp4` + `qc.json`.

### Stage 9 — Delivery & continuity
- Consumes `QcReportReady` (pass or best-available). Move final file to `archive/EP##/`, update `feed.json` (playlist), write episode summary card image (optional Krea 2 gen).
- Run **continuity update**: Showrunner consumes final episode data + summary + unresolved threads → produces `continuity_delta` → run controller applies it to the show's `continuity/state.json` transactionally, bumps `episode`, emits `ContinuityUpdated`.
- Write `runs/EP##/run_report.json` (stages, durations, shot success rate, retakes, QC summary, next-run hook); emit `RunCompleted`.
- **Notification:** the notifier consumer turns `RunCompleted`/`RunProgress` into desktop toast / webhook ("EP12 is ready: 138 shots, 3 retakes, runtime 21:48").

### 9.5 Approval chain & human gates

There is **one dashboard** (M5) and **six approval gates**: **Show → Character → Voice → Story → Shot → Episode**. Every gate is an artifact review with an approve/reject action; whether the gate *blocks* is policy, not code. The creative content at every gate is **AI-generated** — the human reviews and steers, never authors.

**Artifact lifecycle (per gate):**
`draft → pending_review → approved | rejected → (on change) → pending_review → …`

**The six gates:**

| Gate | What you review | Approve = | Reject = | When it matters |
|---|---|---|---|---|
| **0. Show** | An **iterative bootstrap chain** (§9.5a): Concept → Bible → each Character → Scene registry, each a separate approval | Freeze the canon for that step — it becomes a fixed constraint for the next step | Regenerate *that step only* with notes (e.g. "darker tone" at Concept, "replace the mentor" at Character 2); approved steps stay locked | **Bootstrap, one-time per show** (re-opened for a new season/arc or on note-driven reject) |
| **1. Character** | The Krea 2 ref-bank contact sheet (3–6 staged refs per character) | Lock those refs as canonical — they become the identity ground-truth for Krea 2 IP-Adapter *and* H3 ref2va | Regenerate the refs (or adjust the sheet → re-render) | **Bootstrap, per character** (after that character's proposal is approved; re-opened if the user edits the sheet) |
| **2. Voice** | Audition clip(s) per character — same line rendered with `mode: preset` speakers *and/or* `mode: designed` descriptions | Lock the voice (`voice_fingerprint` is frozen, audio cached) | Try another speaker / rewrite the `voice_description` | **Bootstrap, per character** (after their proposal is approved; re-opened only on a voice change) |
| **3. Story** | `plan.json` (synopsis/beats/hook) then `script.json` — the script arrives **already gone through the writers'-room review loop** (Stage 2a), with the slop, continuity, and fan-service reviewers' **separate notes** + revision history attached | Let keyframes + H3 spend GPU hours on it | Edit (regenerate with notes → one more revision round), or add your own notes; residual reviewer notes are visible and can be waived | **Per episode**, before production |
| **4. Shot** | Rendered shot + QC scores in the shot browser | Keep it (it's already in the cut) | `RetakeRequested` with a note (and optionally a changed seed) | **Per shot**, during/after production |
| **5. Episode** | The assembled preview cut | "Air": deliver to `archive/`, update playlist, apply continuity | Rework queue (flagged shots, or re-run) | **Per episode**, after assembly — this is the "air" gate |

### 9.5a Iterative bootstrap (`init-show`, Gate 0)

The show is built **step by step, each step approved or rejected before the next generates**. Rejections regenerate *only* the current step with the user's notes; everything already approved stays locked as constraints. State persists, so the wizard resumes where it left off.

`studio.py init-show --name neon-sutra --brief "…"` creates a new show under `shows/neon-sutra/`; a studio can hold many shows (§8, §17), each with its own chain below.

**The chain (each step = its own event pair):**

```
1. Concept   → ConceptPending → [approve] → next
2. Bible     → BiblePending    → [approve] → next        (conditioned on approved Concept)
3. Character → CharacterProposalPending{char} → [approve] → refs + voice for THAT char → next char
4. Scenes    → SceneRegistryPending → [approve] → BOOTSTRAP COMPLETE → first episode can start
```

### 9.5b Living show: expand while it runs

The studio **keeps running until the user stops it** (no season cap — when arcs exhaust, Development proposes the next arc automatically; continuity persists forever, §17). Shows are also **live-appendable** while producing:

- **New character mid-run:** `studio.py add-character --show neon-sutra` (or the dashboard) → the Showrunner proposes the character → runs the **same bootstrap sub-chain** as Gate 0 step 3: `CharacterProposalPending` → refs (Gate 1) → voice (Gate 2), each approved/rejected. The rest of the show is untouched; the new character becomes available to scripts from the next episode (and can be added to `mature_spec.characters` by the fan_service reviewer's note).
- **New theme / plot direction mid-run:** a theme is just content — the user can add it to the approved bible's arcs (or let Development propose new arcs automatically), and the slop/continuity/fan_service reviewers keep it on the approved canon. No rebuild; the continuity delta and the per-episode Development call absorb it.
- **New scene locations** flow through the existing scene-registry proposal at Stage 3 (Krea 2 renders + caches a scene ref, no approval needed beyond the script review).
- **Removing/retiring a character or arc:** set `status: retired` in the show's YAML; continuity records the exit and the Showrunner writes them out over the next arcs. Retired refs stay archived but stop being scheduled.

1. **Concept** — `init-show --brief "…"` (or `--generate`, zero input) → Showrunner proposes logline, genre, tone, maturity level. Approve → locked.
2. **Bible** — generated from the approved Concept: title, world + rules, arcs + beats, `style_guide`, `runtime_target`, `episodes_per_arc`. Approve → locked.
3. **Characters, one at a time** — the Showrunner proposes the roster (3–5) and processes them sequentially: *Character N proposal* (name, `appearance_canonical`, personality, `traits_for_llm`, `h3_slot`, voice mode + `voice_description`) → approve → immediately bootstrap that character's **refs (Gate 1)** then **voice (Gate 2)** → then propose *Character N+1*. Rejecting Character N does not disturb Characters 1…N−1.
4. **Scene registry** — initial locations with descriptions (the per-episode scene generator reuses/extends these later). Approve → bootstrap is complete; `continuity.state` is initialized and episode 1 can be scheduled.

**Bootstrap granularity (`config/approval.yaml → bootstrap.depth`):**
- `full` (default) — the chain above, one approval per step.
- `coarse` — Concept → Bible → *all characters as one batch* → refs for all → voices for all → scenes. ~4 approvals instead of ~12.
- `auto` — the whole chain auto-approves; review the finished cast/voices post-hoc in the dashboard.

**Master auto-approve switch (`approval.global.auto_approve`):** one toggle that collapses **every** gate (bootstrap steps included) to `auto` — approvals are recorded as `auto_approved: true` and reviewable later, and **nothing ever waits on a human**. The content-policy judge is *not* an approval gate and always runs regardless. The switch can be flipped at any time (set in `approval.yaml` or from the dashboard header), so a show can start fully gated and later be left to run hands-free.

**Two approval modes (per gate, `config/approval.yaml`):**
- **`auto` (default for Shots; sensible for Story in multi-night runs; all bootstrap steps when `auto_approve` is on)** — the artifact proceeds automatically; the human reviews it post-hoc in the morning and can still trigger rework. **Nothing ever blocks the overnight window.**
- **`gated` (default for bootstrap steps, Character, Voice, Episode; opt-in for Story)** — the consumer emits `XxxPendingApproval` and the **downstream stage does not start** until `XxxApproved`. Gated Story is the "don't burn GPU on a script I haven't read" mode. Gated Episode is "no airing without my eyes on it."
- Safety valve: any `gated` gate can carry `auto_approve_after_hours` (default off) so a forgotten approval can't stall a run forever — it degrades to `auto` with a flag in the run report.

**Review strategy that keeps a 132-shot episode sane:**
- Shots are **auto-approved above the QC threshold**; only **hero shots, QC-borderline, and QC-failed** shots land in the human queue. Scene-level batch approve covers the rest.
- Story approval is the highest-leverage human touchpoint (cheap text, shapes everything downstream), so the morning notification leads with it.

**Events:** approvals ride the bus (`bus:approval`): `ConceptPending/Approved/Rejected`, `BiblePending/Approved/Rejected`, `CharacterProposalPending/Approved/Rejected{char}`, `SceneRegistryPending/Approved/Rejected`, `CharacterRefsPending/Approved/Rejected`, `VoiceSamplePending/VoiceApproved/VoiceRejected`, `PlanPending/PlanApproved/PlanRejected`, `ScriptPending/ScriptApproved/ScriptRejected`, `ShotPending/ShotApproved/ShotRejected`, `EpisodePending/EpisodeApproved/EpisodeRejected`. The notifier turns `…Pending` events into toasts/webhooks ("Character 2 is ready for review", "EP12 script is ready for review").

**The dashboard (M5) is the single review surface** for all six gates — see §20.2/M5 and §9.6.

### 9.6 Dashboard (review surface)

A local web app (FastAPI + small frontend, binds `127.0.0.1`) that subscribes to the same events and renders:
- **Show switcher** — the header lists all shows (`shows/*`, §8); every view below is scoped to the selected show, plus an "add character / new show" action (§9.5b).
- **Setup wizard** — the iterative bootstrap chain (§9.5a): a step-by-step deck (Concept → Bible → Character 1 → Character 2 → … → Scenes), each step an approve/reject-with-notes card, with a progress rail and a master **"auto-approve everything"** toggle in the header. *Gate 0.*
- **Episode wall** — episodes with status, QC composite, runtime, approve/air button; the morning landing page.
- **Story view** — `plan` + `script` per episode; edit → "regenerate with notes", approve/reject. *Gate 3.*
- **Characters view** — ref-bank contact sheet per character; lock/reject refs, regenerate, see which shots used which ref. *Gate 1.*
- **Voices view** — audition clips side by side (preset vs designed), pick/lock a voice, rewrite `voice_description`. *Gate 2.*
- **Shot browser** — grid of shots with per-axis QC scores, filters (failed / flagged / hero / scene), approve/reject/retake per shot or in batches. *Gate 4.*
- **Episode player** — watch the assembled cut, jump to a shot's review, approve to air. *Gate 5.*
- **Run log** — stages, retakes, budget, policy events (drill into any `policy_events` block).

Dashboard actions emit the same approval events the pipeline consumes, so human input is just another actor on the bus.

---

## 10. ComfyUI integration detail

### 10.1 Image workflow (Krea 2)

The exact Krea 2 stack is **confirmed from the LanguageLearner project** (the user's own working setup):
- Checkpoint: `krea2TurboNSFWAIO_v10.safetensors` — loaded via **UNETLoader** (Flux-family: `cfg=1.0`, `ConditioningZeroOut` for the negative, ~8 steps, `euler`/`simple`).
- CLIP: `qwen3vl_4b_fp8_scaled.safetensors` (`CLIPLoader type="krea2"`). VAE: `qwen_image_vae.safetensors`. Optional style LoRA (`fedor_bypass.safetensors`).
- Resolution via a **ResolutionSelector** node (aspect ratio + megapixels), not a hardcoded EmptyLatentImage.

Three template workflows, all loaded from **exact exported JSON** (like H3):
1. `workflows/image_keyframe.json` — plain t2i for keyframes/scenes/stills (no character ref). Inputs: `prompt`, `seed`, `aspect_ratio`, `megapixels`, LoRA toggle.
2. `workflows/image_keyframe_ipadapter.json` — the same graph with **IP-Adapter injected** for character consistency (§14.5): `IPAdapterUnifiedLoader` + `CLIPVisionLoader` (ViT-H; ViT-bigG if an Illustrious-based model is used) + `LoadImage(character ref)` + `IPAdapterAdvanced` wired before the KSampler, **`weight ≈ 0.55–0.6`, `end_at = 0.6`** so the text prompt reasserts the background during the last 40% of steps.
3. `workflows/image_character_ref.json` — bootstrap generation of the character ref bank under **reference-sheet discipline**: standing full-body, neutral expression, plain/gray background, no accessories — "staged for IP-Adapter conditioning strength" (§14.5).

**Content guard at image-gen compile time:** the orchestrator prepends an adult descriptor (e.g. "adult woman") and rewrites age-implicating tokens when the policy is `mature` (§13) — proven pattern from LanguageLearner's `daily_generate.py`.
- Outputs: one PNG per job saved to `ComfyUI/output/` → stage consumer moves it into the asset tree.

### 10.2 H3 duration grid
H3 snaps output to `17k + 5` frames at 24 fps. Valid shot durations for episodes:
```
k    :  6     7     8     ...  19     20
frames: 107   124   141   ...  328   345
seconds:4.458 5.167 5.875 ... 13.667 14.375
```
- Rule: `k ∈ [6,20]` ⇒ duration ∈ [4.46, 14.375] s. Longer shots are expensive; default shot length config = **10.125 s (k=14, 243f)** for dialogue scenes, **5.167 s (k=7)** for inserts.
- The orchestrator's `snap_duration()` maps any requested duration to the nearest valid grid value and stores `grid_frames` on the shot.

### 10.3 H3 Director workflow template (`workflows/h3_shot.json`)
Node graph (mirrors the repo's example):
```
UNETLoader (fl2va, "model") ─┐
UNETLoader (ref2va, "model")─┤
CLIPLoader (type=minimax)  ─┼→ MiniMax H3 Director ─┬→ model ─→ BasicGuider ─→ SamplerCustomAdvanced
VAELoader (video vae)      ─┤                       ├→ positive ┘                 │
VAELoader (audio vae)      ─┘                       ├→ latent ────────────────────┘
                                                   ├→ combined_audio → CreateVideo.audio
                                                   └→ fps ───────────→ CreateVideo.fps
```
- `CLIPLoader.type` **must be `minimax`** (a top gotcha).
- Sampler `res_multistep`, scheduler `simple`, ~20 steps, `BasicGuider` (no CFG). For ref-heavy shots try `beta`/`normal`.
- Joint latent → `VAEDecode` (video vae) **and** `VAEDecodeAudio` (audio vae); `CreateVideo` muxes.
- **Refs OFF (fl2va):** `global_prompt`, first/last keyframes on the main track.
- **Refs ON (ref2va):** `@char1..3` slot images + `ref_images`, audio on the Audio track.
- Optional `MiniMax H3 Preview Override` between Director `model` and sampler for live preview (dev mode only).
- The orchestrator edits the template's widget values + `LoadImage` paths, then submits. It never hand-edits the compiled prompt (deterministic compile in the orchestrator).

### 10.4 Compiled H3 prompt (MiniMax notation)
```
subject_definitions: <Subject 1> is the character shown in <Picture 1>.

retention_analysis: Keep the identity, face and clothing of <Subject 1> consistent across every shot.

detailed_description: Live-action, cinematic. [Shot 1] the courier lands on the rooftop, slow push-in
[Shot 2] At 00:01.500, <Subject 1> holds out the package

overall_soundscape: wind, distant sirens
non_diegetic_music: bass drone
```
- First shot carries no timestamp; later cuts carry strictly increasing `MM:SS.mmm`.
- Empty sound/music sections are omitted entirely.
- `@charN` → `<Subject N>`; ComfyUI tokenizer labels images `<Picture i>`; `subject_definitions` binds them.
- The orchestrator compiles this deterministically from `shot_list` (no LLM in the loop, so the prompt shown in logs == the prompt the model receives).

### 10.5 Retake via Retake Stitch
- On a QC-failing shot, orchestrator re-submits with **Retake Mode ON** over the failing range; Director anchors `first_frame`/`last_frame` to the base video's own frames either side; **MiniMax H3 Retake Stitch** splices head + retake + tail and resamples to 24 fps.

### 10.6 H3 hard limits (enforced at Stage 4)
| Limit | Value |
|---|---|
| Output envelope | 4–15 s @ 24 fps |
| Reference images | ≤ 9 (incl. the 3 char slots) |
| Reference videos | ≤ 3, each 2–15 s, ≤ 15 s total |
| Reference audio | ≤ 3 clips |
| All reference types | ≤ 12 files total |
| Aspect ratios | 21:9, 16:9, 4:3, 1:1, 3:4, 9:16 |
| Native canvas | 768 px short edge, capped 768×1344 |
| Keyframes | first and last frame only |

### 10.7 Headless operation
- ComfyUI instances are started by their local processes (`studio.py serve` on B for Krea 2; the renderer agent `studio.py renderer` on A for H3, `comfy.manage_lifecycle: true`) or as Windows services. The agent waits for `/system_stats` healthy before submitting.
- Uploaded refs go to `ComfyUI/input/whatdreamscost/` (same folder LTX Director uses). Orchestrator writes into a per-run subfolder to avoid collisions.

---

## 11. LM Studio integration

### 11.1 Transport
- Base URL `http://127.0.0.1:1234/v1`; request schema per `/v1/chat/completions`.
- All creative calls use `response_format={"type":"json_object"}` and the stage schema from `schema/`.
- A thin `client.py` wrapper retries on 429/timeout with exponential backoff; LM Studio is single-tenant so a serial queue is expected.

### 11.2 Role → model config (`config/llm.yaml`)
| Role | Purpose | Suggested class | Temp | Context |
|---|---|---|---|---|
| `showrunner` | bible/dev/script/continuity | large instruct (e.g. 32B Q4); **precedent: LanguageLearner runs `dolphin-2.9.3-mistral-nemo-12b` (uncensored 12B) for mature creative writing** | 0.8 | 32k+ |
| `director` | shot-level polish (optional; orchestrator compile is default) | mid | 0.7 | 16k |
| `judge` | content policy + QC sanity | small instruct | 0.1 | 8k |
| `slop_reviewer` | AI-slop detection on scripts/dialogue (Stage 2a) | mid instruct | 0.2 | 16k |
| `continuity_reviewer` | script↔continuity cross-check (Stage 2a) | mid instruct | 0.2 | 16k |
| `fan_service_reviewer` | maturity-delivery vs `mature_spec` (Stage 2a/3/8) | mid instruct | 0.2 | 16k |
| `describer` | image ref descriptions for ref banks | vision instruct (e.g. Qwen2.5-VL) | 0.4 | 16k |

All model names come from the user's LM Studio catalog; the pipeline fails fast with a clear message if a configured model isn't loaded.

### 11.3 Prompt scaffolding
- **System prompt** = the show's `bible.yaml` (rendered) + its continuity state + character sheets + `content_policy` + fictional-only directive + JSON output contract. Built once per run (per show), cached.
- **Continuity injection:** the top of every creative prompt carries `continuity.state` and the rule *"Never contradict the state below; extend it, don't rewrite it."*
- **Determinism aids:** each stage sends `seed` in the JSON request; LM Studio/OpenAI-compatible servers may not honor it, so the orchestrator records the raw response as `artifact` for audit regardless.

### 11.4 VRAM interlock
- Managed by the per-node **GPU Manager** (§15.4), the same pattern LanguageLearner proves: stage consumers wrap LLM work in `gpu_manager.acquire(ServiceType.LLM)`; before Krea 2 or H3 runs, the manager evicts the LLM (`lms unload --all`) and on release evicts ComfyUI. `llm.gpu_offload: false` drops the LLM to CPU during Stage 5 as a fallback. Default on single-GPU: creative stages (1–2) run fully, then LLM evicted before Stage 3 keyframes and Stage 5 H3.

---

## 12. Voice & dialogue system

### 12.1 VoiceActor registry (`shows/<show_id>/voices/`)

**Concrete engine: Qwen3-TTS 12Hz 1.7B** (the user's working setup in LanguageLearner) — two local models:
- `Qwen3-TTS-12Hz-1.7B-CustomVoice` → `generate_custom_voice(text, language, speaker)` — a fixed pool of speakers: `Aiden, Dylan, Eric, Ono_anna, Ryan, Serena, Sohee, Uncle_fu, Vivian`.
- `Qwen3-TTS-12Hz-1.7B-VoiceDesign` → `generate_voice_design(text, language, instruct=voice_description)` — a **unique voice synthesized from a free-text description**.

Per character, `mode` picks one (`shows/<show_id>/voices/*.yaml`, §8.3):
- `mode: preset` → assign one speaker from the pool. Cheap, deterministic, stable across episodes.
- `mode: designed` → write a `voice_description` (e.g. "low, husky, sardonic woman in her 20s, subtle electronic reverb"). This is the anime-cast approach: every character gets a bespoke, persistent voice. **Vocal consistency comes from the description being stable** — `voice_fingerprint = sha256(speaker|voice_description)` is the cache key and QC identity handle; editing the description deliberately retires the old voice.

Adapter contract: `synthesize(text, voice_cfg) -> wav + duration_s`. Output is cached in the asset registry by fingerprint (idempotent TTS, like the AudioCache table in LanguageLearner).

### 12.2 Dialogue pipeline
1. Script stage writes lines with target `start_s`/`end_s`.
2. Stage 6 resolves each line's VoiceActor from the character's `voice` + `mode`, synthesizes to `assets/EP##/dialogue/<id>__<char>.wav`.
3. Per-shot dialogue track assembled with silence padding.
4. Delivered to H3 as an audio reference **or** muxed in ffmpeg (§9 Stage 6).

### 12.3 Lip-sync via reference audio
- Feeding the shot's dialogue track to the Director Audio track (`h3_reference` mode) is what makes characters speak: H3 generates video and audio jointly, so the audio latent conditions mouth/torso motion. This is in-model behavior, **not** a separate lip-sync model.
- Quality expectations: best when the speaking character is on camera and reasonably close (medium/close shot); wide or off-camera lines can be left to `ffmpeg_mux`. The script stage tags each line with `on_camera: true|false`; the orchestrator uses it to choose the audio mode (§9 Stage 6) and to flag wide shots for the QC judge.

### 12.4 Consistency
- Same character always resolves to the same `voice_fingerprint`; the registry guarantees a character never switches voice mid-series (voice drift not auto-detected in v0; QC judge optionally samples and flags).
- **QC lip-sync check (v0, cheap):** for `h3_reference` shots with `on_camera` dialogue, sample 2–3 frames at peak speech and have the local vision model (`describer`) verify the mouth is open/different from a resting frame. Failures degrade that shot to `ffmpeg_mux` (dialogue still lands; only the in-model lip motion is lost). A dedicated Wav2Lip-style pass is out of scope in v0 and noted as a possible M5+ enhancement.
- **Vocal QC:** for `mode: designed` characters, the judge occasionally compares a sample line's prosody/energy to the `voice_description` as a sanity check (phase 2).

---

## 13. Content policy & local-only guarantees

- `content_policy.yaml`:
  ```yaml
  maturity: mature            # clean | mature
  always_block:
    - "real identifiable persons"
    - "minors"
    - "non-consensual framing"
    - "real brands/trademarks in keyframes"
  judge_model: local_judge
  scan_points: [script, keyframe_prompts, h3_prompts, dialogue]
  ```
- The **judge** (local small LLM, temp 0.1) reviews each artifact at the listed scan points. Verdict `pass`/`block` + reason; blocks write a `policy_events` row and **hard-stop the stage** with the artifact + reason in the report. This is the local guardrail that makes "mature" support safe to run unattended.
- **Two floors, both enforced:** the judge enforces the *hard floor* (what must never appear — real persons, minors, non-consent, brands); the **fan_service reviewer** (Stage 2a/3/8) enforces the *creative floor* (the mature content the approved `mature_spec` promised). A scene that is legal but flat, or an episode that under-delivers its quota, fails `fan_service` — not the judge. The judge is never an approval gate; `fan_service` is a normal reviewer whose notes drive revisions.
- **Network guarantee:** no HTTP call in the generative path targets anything but `127.0.0.1`. Optional Windows Firewall rule blocks pipeline processes from LAN/WAN egress entirely (phase 2 hardening).
- All characters are declared fictional in every system prompt and enforced by the judge.

---

## 14. Continuity & character identity

- **Long-term memory** lives in the show's `continuity/state.json` (§8.4), updated transactionally after each delivered episode.
- **Identity across shots** relies on (1) `appearance_canonical` in the character sheet, (2) cached Krea 2 ref bank, (3) H3 `ref2va` references + `subject_definitions`/`retention_analysis` lines, (4) identical keyframe prompting from the orchestrator.
- **Identity across episodes** relies on the same ref bank never being regenerated unless the user edits the sheet (checksummed ref images).
- The Showrunner's continuity-delta prompt explicitly asks for: world facts changed, character status changes, plotline updates, new unresolved threads, and a `continuity_rules` dedup so contradictions don't accumulate.

### 14.5 Character consistency playbook (distilled from the LanguageLearner project)

Six mechanisms work **together**; no single one is enough:

1. **Canonical description, always injected.** `appearance_canonical` is embedded verbatim into *every* Krea 2 and H3 prompt the character appears in. It never varies. (`character_description` in LanguageLearner plays this role.)
2. **Structured prompt decomposition + shared episode state.** Image prompts are built from independent components — **pose / action / clothes / setting** — generated separately by the LLM. The **clothes and setting are frozen for the whole episode** and only change via an explicit "keep or change outfit?" LLM decision (changed on location change / time-skip / story beat, not randomly). This is prompt-state management: the mutable bits are the per-shot pose/action, not the identity.
3. **Character reference bank under reference-sheet discipline.** At bootstrap, Krea 2 renders 3–6 refs per character (full-body front/side, close-up) staged **specifically for conditioning**: neutral pose, plain/gray background, minimal accessories. These refs feed **both** consumers — IP-Adapter on Krea 2 keyframes *and* `ref2va` on H3 shots — so both models are steered by the same canonical imagery.
4. **IP-Adapter on the Krea 2 keyframe path.** `image_keyframe_ipadapter.json` wires the character ref through `IPAdapterUnifiedLoader → IPAdapterAdvanced`, `weight ≈ 0.55–0.6`, **`end_at = 0.6`** (text prompt reclaims the background for the final 40% of steps). This is what makes a keyframe *of Kiyo* rather than a generic woman.
5. **`ref2va` on the H3 shot path.** The same refs bind to `@charN` slots → `<Subject N>` + `subject_definitions`/`retention_analysis` (§10.4), so the video honors the keyframe identity across the cut. **User experience (confirmed): H3's character consistency is excellent *provided the reference images are good* — so the ref bank, not the model, is the lever that matters.**
6. **Deterministic per-character voice.** `mode: preset` (fixed Qwen3-TTS speaker) or `mode: designed` (stable `voice_description`, `voice_fingerprint` cache key). A character's voice is as stable as its face (§12.1).

### 14.6 Reference-image quality bar (the real lever for H3 identity)

Because H3's identity fidelity tracks the quality of the refs it's given, the pipeline treats the ref bank as first-class, quality-controlled assets:

- **Per-ref minimums** (checked at bootstrap and on every promotion):
  - Character face large, **near-front-facing**, sharp, evenly lit; no occlusion (hair strand over eye, shadows across face).
  - Neutral or mild expression; no heavy distortion from pose.
  - Matches `appearance_canonical` (verified by the `describer` vision model against the description).
  - **Style-consistent with the show** — refs are generated with the same Krea 2 style_guide the keyframes use, so H3 sees the character *in the show's look*, not a divergent sheet.
  - Resolution ≥ keyframe resolution (1344×768 or better); aspect 16:9 (or a square face-crop variant).
  - **Internal consistency:** all refs for one character agree with each other (judge compares pairwise); a bad ref is rejected, not promoted.
- **Self-reinforcing ref bank (in-show stills).** The seed refs come from Krea 2. As the show renders, **QC-verified frames of a character on screen become additional refs** (promoted to `ref_images`): a sharp frame from a rendered shot is *already in the show's lighting/style/outfit*, which is exactly what H3 conditions best on. Promotion rules:
  - Only frames that pass the QC identity axis and are crisp (no motion blur) qualify.
  - A cap per character (e.g. ≤ 9 total refs, matching H3's image-ref limit); evict oldest/weakest on promotion.
  - The Krea 2 seed sheet stays as the baseline so a transient bad render can't entrench itself (errors are never promoted).
- **Effect:** identity gets *better* over a season instead of drifting, and the ref bank doubles as the QC identity ground-truth.

**Who changes what, deliberately:**
- **Default: everything is AI-generated and approved.** The Showrunner writes the bible, cast, voices, and every script; the human reviews at the gates (§9.5) and can send anything back with notes for regeneration. Hand-editing generated YAML is always allowed as an *override* — but never required.
- User edits a character sheet → ref bank regenerates + `appearance_canonical` updates → both Krea 2 and H3 consume the new canon.
- Showrunner changes an outfit **in-story** → the clothes-decision prompt records it in `character.outfit_state` (§8.2) and continuity state, so the change persists across episodes instead of flickering.
- Everything else (pose, action, lighting) varies per shot without touching identity.

**Verification:** QC Layer 1's `identity` axis compares rendered frames against the ref bank (Stage 8); cross-episode drift is the judge's cross-shot pass. Consistency is therefore *measured*, not assumed — and the measured-clean frames feed §14.6's promotion loop.

---

## 15. Throughput & capacity model

### 15.1 Honest math (single mid/high-end GPU, 1344×768 16:9)
- H3 per-shot render: **5–15 min** (avg ~9 min for 10 s @ 768 short edge with offload).
- Krea 2 keyframe: ~30–90 s each (×2 per shot → ~1–3 min/shot amortized).
- TTS + assembly + LLM: negligible (< 5% of wall clock).
- 22 min @ avg 10 s/shot ≈ **132 shots** → H3 alone ≈ **20 hours**; total ≈ **22–24 h**.
- **Two-node variant (see §15.5):** with the 4060 Ti dedicated to H3 and the 1660 Super handling image gen + LLM + TTS + assembly in parallel, wall-clock ≈ **H3 time alone ≈ 20 h** — the long pole becomes the only pole.

### 15.2 Operating modes (config `pipeline.mode`)
| Mode | Meaning | Outcome |
|---|---|---|
| `overnight` (default) | Runs nightly window (e.g. 22:00→06:00, budget 480 min) | Resumes EP each night; ~132 shots / ~20 h ⇒ episode lands every **2–3 nights**. Progress report each morning. |
| `continuous` | Pipeline stays armed 24/7; the timer source re-publishes `RunRequested` every N hours | ~1 episode / day if GPU is dedicated. |
| `hybrid` | Overnight + weekend long runs | Balances power cost and cadence. |

### 15.3 Parallelism
- `pipeline.shot_workers: 1` default. Increase with more VRAM or a second ComfyUI instance on another GPU (`comfy.instances: [{url, gpu}]`). Shots are independent once keyframes exist, so the pool scales linearly.
- Serialization rule on a single GPU: keyframe stage fully completes before the first H3 render (§15.4), preserving the single-resident-checkpoint principle.
- **Producer-consumer (two-node, §15.5):** keyframes and audio for shot *N+k* are produced on the worker machine **while** H3 renders shot *N* on the renderer. With image gen at ~1–3 min/shot and H3 at ~9 min/shot, the worker stays comfortably ahead and the renderer never idles. This is the default topology for the user's hardware.

### 15.4 GPU resource plan (single-GPU, i.e. the 4060 Ti node alone)
| Phase | Resident on GPU | Notes |
|---|---|---|
| Stage 1–2 (LLM) | LM Studio model | Creative writing; low VRAM use |
| Stage 3 (Krea 2) | Krea 2 checkpoint | LM Studio → CPU/evicted first |
| Stage 5 (H3) | H3 fl2va/ref2va + TE + VAEs | Krea 2 evicted first; LLM on CPU |
| Stage 6–9 | none | light CPU work |

**Enforcement = a GPU Manager per node** (the proven pattern from LanguageLearner's `gpu_manager.py`): each node exposes an acquire/release context manager over `ServiceType {LLM, TTS, COMFYUI, STT}` with VRAM budgets, load-on-acquire / unload-on-release (with warmup delays and port checks). Only one GPU-heavy service is resident at a time on a node; the stage consumers wrap their work in `async with gpu_manager.acquire(...)`. On the 1660 Super worker this makes LLM ↔ TTS ↔ Krea 2 swap cleanly; on the 4060 Ti renderer, H3 holds the GPU exclusively.
- Config: `llm.evict_before_render`, `ker2.unload_after_batch`, `h3.fp32_vae` (grey-frame gotcha, §19).
- LM Studio itself is managed via the `lms` CLI (`lms server start`, `lms load <model> --gpu max`, `lms unload --all`) with health-polling — exactly how LanguageLearner drives it.

### 15.5 Two-machine topology (4060 Ti 16 GB + 1660 Super 6 GB)

**Feasibility verdict:** H3 needs ~16 GB VRAM even with offloading and the 1660 Super has **no tensor cores** (GTX), so it should **never run H3** — it would thrash CPU-offload and likely OOM. But every other department fits it comfortably, which is exactly the split we want.

| Node | Hardware | Department | Notes |
|---|---|---|---|
| **A — renderer** | 4060 Ti 16 GB | H3 shot production only (Stage 5 + H3 retakes) | GPU kept 100% for H3; VRAM/CPU/RAM fully free of LLM/TTS/image work |
| **B — worker** | 1660 Super 6 GB | Showrunner + judge LLM (LM Studio), Krea 2 image gen, TTS, ffmpeg assembly, dashboard | All of Stages 1–4, 6–9; GPU for Krea 2 (if it fits), CPU for LLM/TTS |

Work split by stage:

| Stage | Runs on | Why |
|---|---|---|
| 1–2 Creative (LLM) | B (CPU or small GPU offload) | Text is tiny; keeps A's VRAM free. A 14B Q4 ≈ 10–20 tok/s on B's CPU is plenty for nightly output. |
| 3 Keyframes (Krea 2) | B | SD1.5-class fits 6 GB easily; SDXL fits with `--lowvram` (slow); Flux GGUF only if quantized. **Verify at M0 — this decides B's role in Stage 3.** |
| 4 Validation | B (orchestrator) | pure CPU |
| 5 Shots (H3) | A | the long pole; never run on B |
| 6 TTS | B | Piper et al. run real-time on CPU |
| 7 Assembly | B | ffmpeg CPU |
| 8 QC | B (judge on CPU) + ffprobe | sampler frames pulled from A's share |
| 9 Delivery | B | archiving + continuity (orchestrator) |

**Sync / transport (Windows):**
- **Control plane = the event bus** (§6.4): broker runs on node B (`bus.url` in `bus.yaml`); renderer A's agent subscribes to `bus:shots` and publishes results. No orchestrator-polling-the-renderer.
- **Shared artifact store:** an SMB share (e.g. `\\MACHINE-B\anime\share`) mounted on both nodes. Producers write to the share and reference paths in event payloads; consumers read them. This is the simplest, most auditable option — no bespoke file-transfer protocol.
- **Network auth required:** LAN-only listeners; broker password (`bus.password`) + firewall rules for port 6379 (broker on B) and the SMB share. ComfyUI stays loopback-bound on both nodes — no HTTP surface to expose. Never expose to WAN.
- **Fallback:** if the share is down, Stage 3/6/9 can run on A after H3 finishes (degraded, no pipeline overlap) — `pipeline.fallback_local: true`. If the broker is down, nothing moves (see §19).

**Single-machine fallback:** with `pipeline.nodes: {renderer: null}`, the whole pipeline runs on A as before (§15.4). The two-node split is purely additive config.

**Expected cadence (two-node, continuous mode):** ~20 h/episode with H3 saturated → **~1 episode/day**, or a 22-min episode inside one long weekend/overnight pair. See §15.2.

---

## 16. State machine, persistence, resumability

### 16.1 Run lifecycle
`booting → developing → scripted → storyboarded → validated → producing → assembled → qc → delivered | failed | aborted`

Artifacts can pause at `pending_review` where a gate is `gated` (§9.5) — the run shows `blocked_on: script` etc., and resumes when the approval event arrives (or `auto_approve_after_hours` fires).

Transitions are driven by the bus events of §6.4 and recorded in SQLite (the source of truth). The event log and the DB are reconciled by `event_id`.

### 16.2 Commit discipline
- Each stage ends in one DB transaction: stage row `status=complete`, asset rows, and any state files written **atomically** (temp file + rename) — **then** the trigger event is acked. A crash between commit and ack is safe: the re-delivered event dedups on `event_id` and skips.
- `input_hash` (sha256 over consumed inputs) lets a re-run detect "nothing changed" and skip.

### 16.3 Resume
- On boot, the run controller finds the newest incomplete run. It re-enters at the first stage whose `status != complete`; consumer groups automatically redeliver any unacked events from where the process died, so in-flight work resumes without manual intervention.
- If a half-written video file exists, it's treated as corrupt and the shot re-renders (idempotent by asset id).
- Renderer A being offline is not a resume problem: `ShotScheduled` events simply stay unacked in `bus:shots` and are delivered when A's agent comes back.

### 16.4 Crash safety
- SQLite `journal_mode=WAL`; a single writer (the run controller on B).
- Bus: streams are append-only and durable; consumer groups track delivery; `bus:dead` catches poison events.
- Heartbeat: consumers publish to `bus:system`; stale runs (> 2× budget) are marked `aborted` with a report, not silently resumed.

---

## 17. Event source & scheduler (Windows)

The pipeline is **event-triggered**, not cron-driven:
- **Timer source (primary):** `python studio.py serve` runs an in-process APScheduler on node B that publishes a `RunRequested{show_id, episode, mode, budget_min}` at `pipeline.nightly_time` (default 02:00) with optional wake-from-sleep — **once per enabled show** (§8/§9.5b). This is the only "schedule" left.
- **Multi-show budget:** each show config carries `schedule.enabled` and optionally `budget_min`; the BudgetGate splits the nightly window across the enabled shows (default: equal shares; or `budget_min` per show). The pipeline **runs forever until stopped** — when a show's arcs exhaust, Development proposes the next arc automatically; no season cap (§9.5b).
- **Alarm clock (optional):** a one-line Windows Task Scheduler task may be used purely to launch `studio.py serve` at boot/02:00 (or `studio.py request-run --show X` to fire a single event). It does not drive the pipeline; the bus does.
- **Node startup:** `studio.py serve` on B starts: broker health check → DB → the stage consumers (dev/script/keyframes/dialogue/assembly/qc/delivery) → notifier. On A, `studio.py renderer` runs the renderer agent (subscribes `bus:shots`) and manages its H3 ComfyUI (`comfy.manage_lifecycle: true`).
- **LM Studio** (B) must be running with configured models loaded (manual, or `--launch-on-boot` + wait-for-healthy).
- **Preconditions at serve-time:** SMB share mounted, broker reachable (B) + renderer reachable (A) + API key valid, ComfyUI `/system_stats` healthy.
- **Guard:** `studio.lock` (per node); overlapping `RunRequested` deduped by `event_id`.
- **Notifications:** the notifier consumer turns `RunProgress`/`RunCompleted` into Windows toast (`winotify`) or a local webhook.

---

## 18. Security & privacy

- No API keys in the repo; the only credentials are local (ComfyUI/LM Studio) plus the **Redis broker password** and the **remote-agent token** (stored in `config/remotes.yaml`, **gitignored** — ship `remotes.yaml.example` instead; `controller.token` is the shared secret, per-role tokens override it).
- **Two-node hardening:** broker binds the LAN interface but is firewall-restricted to the worker node's peers (only A's agent connects); SMB share is credential-gated to the pipeline accounts; ComfyUI and LM Studio stay loopback-bound on both nodes. The **remote-agent** is token-gated (Bearer, constant-time compare) and should be firewall-scoped to the controller's IP (`agent.bind`); with an empty token it runs in dev mode only. Optional full egress block for pipeline processes on both nodes.
- Generated archives are local-only by default; if the user adds remote delivery later, it must be opt-in and the content policy must be re-audited.
- Logs never include full prompts for blocked artifacts by default (privacy); a `--debug-prompts` flag overrides for troubleshooting.

---

## 19. Failure & recovery matrix

| Failure | Symptom | Recovery |
|---|---|---|
| Broker down | consumers idle / reconnect errors | stage consumers retry with backoff and surface `NodeDown` heartbeats; events stay durable in streams; a broker monitor toasts when it returns. No data loss — pipeline resumes on reconnect |
| ComfyUI down | renderer agent health poll fails | agent waits/retries `comfy.startup_retries`; `ShotScheduled` events stay unacked (auto-recovered on restart); abort run with clear report after timeout |
| H3 VAE grey/NaN video | flat grey frames, audio fine | run ComfyUI with `--fp32-vae` (fp32 is the only alt; `--cpu-vae` last resort); auto-retake shot |
| H3 OOM | job fail on VRAM | lower res to 480 short edge, then `duration`; drop to pruned fp8/int8 checkpoints |
| `clip input is invalid` | garbage output | CLIPLoader type must be `minimax` (template lock) |
| `neither model input connected` | Director error | wire both UNETLoaders; template guarantees this |
| Duration mismatch | QC fail | auto-retake (grid re-snap) |
| LM Studio not loaded | 429/timeout | fail-fast message listing required models |
| Ref count overflow | Stage 4 fail | auto-drop least-important ref + warning row |
| Script JSON malformed | Stage 2 fail | 2 repair retries, then abort (cheap stage) |
| Keyframe gen fails | Stage 3 fail | 2 retries w/ new seed, then abort episode |
| Shot render failed ×3 | shot `failed` | episode completes without shot; QC report flags it; next run offers targeted re-render |

---

## 20. Repository layout & build milestones

### 20.1 Proposed layout
```
anime/
├── DESIGN.md                  # this document
├── studio.py                  # CLI: serve | renderer | request-run | status | verify | init-show | add-character
├── studio/
│   ├── bus/
│   │   ├── broker.py          # Redis Streams transport (NATS/RabbitMQ adapter slots)
│   │   ├── events.py          # event catalog + envelope schema (§6.4)
│   │   └── budget_gate.py     # BudgetExhausted/backpressure consumer
│   ├── run_controller.py      # run lifecycle, resume, lock, heartbeat
│   ├── renderer_agent.py      # node A: ShotScheduled → ComfyUI → ShotRendered/Failed
│   ├── gpu_manager.py         # per-node exclusive-GPU acquire/release (LLM/TTS/ComfyUI) — §15.4
│   ├── stages/                # stage_1_development.py ... stage_9_delivery.py (consumers)
│   │                          # + stage_2a_script_review.py (writers'-room review loop)
│   ├── clients/
│   │   ├── comfy.py           # REST/WS client + workflow submission
│   │   ├── lmstudio.py        # OpenAI-compatible client + retries
│   │   ├── tts.py             # engine adapters
│   │   └── ffmpeg.py
│   ├── compile/
│   │   ├── h3_prompt.py       # deterministic H3-notation compiler
│   │   └── durations.py       # 17k+5 grid snapping
│   ├── schema/                # JSON schemas for every stage artifact
│   ├── db.py                  # SQLite access
│   └── policy.py              # content-policy judge hook
├── workflows/
│   ├── image_keyframe.json         # Krea 2 t2i (UNETLoader, krea2TurboNSFWAIO_v10) — exact exported JSON
│   ├── image_keyframe_ipadapter.json  # same graph + IP-Adapter character refs (§10.1, §14.5)
│   ├── image_character_ref.json    # bootstrap ref-bank generation (reference-sheet staging)
│   └── h3_shot.json           # H3 Director template (Refs ON/OFF variants)
├── config/
│   ├── pipeline.yaml          # modes, budgets, workers, resolutions, nightly_time
│   ├── bus.yaml               # provider, url, password, groups, broker host (node B)
│   ├── comfy.yaml
│   ├── qc.yaml                # QC thresholds (anchor SSIM, composite/identity floors, sample rates)
│   ├── approval.yaml          # global.auto_approve, bootstrap.depth, per-gate modes + auto_approve_after_hours (§9.5/§9.5a)
│   ├── reviewers.yaml         # script-review roles (slop, continuity, fan_service), max_revisions, thresholds (§9 Stage 2a)
│   └── llm.yaml
├── shows/                      # MULTI-SHOW (§8): one universe per subfolder
│   └── <show_id>/              # bible.yaml, characters/, voices/, scenes/, continuity/,
│                               #   runs/EP##/, assets/EP##/, archive/EP##/ + feed.json
├── schema_docs/               # exported schemas for reference
```

### 20.2 Milestones
| Milestone | Scope | Exit criteria |
|---|---|---|
| M0 — Scaffold | repo, config, DB, **bus broker (Redis Streams) bring-up**, CLI (`serve`/`renderer`/`request-run`), clients, smoke test each backend + a round-trip event | `studio.py verify` reports healthy bus, ComfyUI (both workflows), LM Studio, TTS, ffmpeg; an `emit → consume → ack` test passes across both nodes |
| M1 — "One shot" | **Gate 0: iterative bootstrap** (`init-show --generate/--brief` → Concept → Bible → per-character proposals + refs + voices → Scenes, §9.5a) → event-driven Stages 0–6 minimal: one scripted shot → **character ref bank (Krea 2 reference-sheet) + IP-Adapter keyframes** → one H3 render w/ dialogue audio → mux (all as events) | A single valid ~10 s clip with dialogue, **character visibly consistent with the ref bank** (QC identity axis pass), produced end-to-end through the bus; the bootstrap chain (incl. a rejection→regenerate and the auto-approve toggle) works through the dashboard |
| M2 — Episode loop | Stages 0–9 **incl. the Stage 2a writers'-room review loop** (slop + continuity + fan-service reviewers, separate notes, revision loop) with resume + budget; 8–10 shots | A complete multi-shot episode MP4 + QC report; all reviewers' notes visible at the Story gate and revision history recorded |
| M3 — Continuity | continuity state + deltas + ref banks; EP2 ≠ EP1 in plot, same characters | Two consecutive episodes with stable identity |
| M4 — Scale & QC | two-node producer-consumer (§15.5), retake budget, parallel workers, multi-night resume, notifications | 22-min episode lands unattended in ≤ 3 nights (2-node: ~1/day) |
| M5 — Dashboard | local web UI implementing **all six approval gates** (§9.5/§9.6): show-proposal pitch deck, episode wall + air gate, story view, character ref-bank review, voice auditions, shot browser with QC filters + retake, run log | Full review-and-air workflow without touching files; approval events round-trip on the bus and gated runs block/resume correctly |

---

## 21. Definition of done (acceptance)

A nightly run is **successful** when:
1. The episode for the correct number was produced from the bible/continuity state.
2. Every committed asset has a registry row; no asset is orphaned or duplicated.
3. All shots passed QC (or are explicitly flagged in the report with retakes spent).
4. The final MP4 is the correct length (±3 s of target), playable, and the audio track is non-silent.
5. Content-policy scan passed every artifact (rows in `policy_events`, verdict pass).
6. Continuity state advanced exactly one episode and contains the delta, with no contradictions added.
7. The run report is complete and a crash at any point (consumer, renderer, or broker) would have been cleanly resumed from the event log + DB state.
8. Every `gated` approval gate (Character/Voice/Story/Shot/Episode per `approval.yaml`) was either approved or auto-approved per policy; no gated artifact advanced without a recorded decision.

---

## 22. Open questions (to resolve before implementation)

1. **Krea 2 specifics (mostly resolved)** — confirmed from the user's LanguageLearner setup: checkpoint `krea2TurboNSFWAIO_v10.safetensors`, CLIP `qwen3vl_4b_fp8_scaled.safetensors` (type `krea2`), VAE `qwen_image_vae.safetensors`, optional LoRA `fedor_bypass.safetensors`, `ConditioningZeroOut` negative, cfg 1.0, ~8 steps (§10.1). **Remaining:** confirm that exact checkpoint is also the one to use for the anime show, and whether to keep the style LoRA.
2. **Two-node roles (resolved as default in §15.5)** — A = 4060 Ti 16 GB (H3 only), B = 1660 Super 6 GB (orchestrator + LLM + TTS + assembly + Krea 2). Open sub-questions: (a) does Krea 2 fit/perform on 6 GB (it's Flux-family — needs quant/offload; verify at M0, else image gen falls back to A between H3 batches); (b) confirm SMB share vs a small REST artifact endpoint on B — SMB assumed, verify both machines are domain/homegroup-joined.
3. **LM Studio models** — LanguageLearner precedent: `dolphin-2.9.3-mistral-nemo-12b` (uncensored, for mature writing) + a validation model. Decide final `showrunner`/`judge`/`describer` picks; does the catalog include a vision model (Qwen2.5-VL)? Decide CPU-vs-GPU offload per node (§15.5).
4. **TTS engine (resolved)** — **Qwen3-TTS 12Hz 1.7B** (`CustomVoice` + `VoiceDesign`) confirmed present in the user's setup. Open: per-character `mode: preset` vs `mode: designed` assignments (§12.1), and whether the 2 TTS models (~7 GB VRAM together) run on node B's 6 GB or get pushed to CPU/sequential.
5. **Dialogue vs H3 audio** — validate in M1 how reliably `h3_reference` produces lip-sync at the user's resolution/shot types; the QC lip-sync check (§12.3) will tune the `on_camera` → mode policy empirically.
6. **OP/ED cards & title sequence** — does the user want generated OP/ED stills + stingers, or text cards only?
7. **Episode runtime** — is 22 min a hard target or should the Showrunner be allowed 15–25 min flexibility for shot-count feasibility?
8. **Aspect ratio** — 16:9 confirmed for episode, or do they want a 9:16 cutdown of each episode for social?
9. **Auto-air** — resolved by the approval-chain design (§9.5): the **Episode gate defaults to `gated`** (no airing without a human watching the cut); every other gate defaults to `auto` so overnight production never blocks. Per-gate overrides live in `approval.yaml`. Confirm the user wants Episode-gated as the default.
10. **Budget values** — default nightly window (suggest 02:00, 480 min budget) and max shots/night.
11. **Broker choice** — Redis Streams is the design default (§6.4). Confirm availability on Windows: native Redis via Memurai, or Redis in Docker Desktop/WSL; or prefer NATS JetStream (single binary, lighter)? Settled at M0.
12. **`studio.py serve` daemon** — run as a Windows service or a startup task? (Needed to host the timer source + stage consumers continuously; currently spec'd as a startup task on B.)
13. **Ref-bank promotion tuning (§14.6)** — cap per character (default ≤ 9, H3 limit), eviction policy, and promotion strictness to be calibrated at M1/M3 so the bank improves identity without entrenching a bad render.
14. **Bootstrap depth + auto-approve (Gate 0/§9.5a)** — `bootstrap.depth: full | coarse | auto` and the master `approval.global.auto_approve` switch cover both extremes (step-by-step review vs fully hands-free). Confirm the user's default: likely `full` with the auto toggle available, and whether a reject should regenerate automatically or wait for explicit "retry".
15. **Reviewer tuning (Stage 2a)** — calibrate the slop reviewer's rubric and threshold, `continuity_reviewer` severity handling, the **fan_service reviewer's quota enforcement** (how strictly the `mature_spec` quotas gate an episode, and how it interacts with the Episode air gate), and `max_revisions` at M2 against real scripts. Consider whether a fourth `structure/pacing` reviewer should be added to `reviewers.yaml` (the design is extensible by list).
16. **Anime style source (resolved)** — Krea 2 renders all anime styles (cel / 3D / 2.5D / 80s / 90s); it is the image generator. The show's look is chosen by `style_guide` in the bible (AI-proposed at Gate 0, user-approved). No separate anime checkpoint needed; the exact `style_guide` phrasing is tuned at M1.
17. **Multi-show support (resolved)** — multi-show is the design (§8, §17): `shows/<show_id>/` per universe, per-show episode numbering, `show_id` on every event/DB row, per-show budget shares, dashboard show switcher. `init-show --name <slug>` creates a show.
18. **Arc/season lifecycle (resolved)** — the studio **runs until stopped**. No season cap: when arcs exhaust, Development proposes the next arc automatically; continuity persists forever. Shows are live-appendable: `studio.py add-character --show X` reuses the bootstrap sub-chain, and new themes/plots flow through normal Development + reviewers (§9.5b).
19. **Episode language (resolved) & subtitles (open)** — **English only**: `bible.language: "en"`, Qwen3-TTS English voices, English dialogue/narration/subs. **Remaining:** burned-in subtitles vs `.srt` sidecars (if any) — affects the assembly step (§9 Stage 7).
20. **Character-consistency ceiling (LoRA vs IP-Adapter)** — IP-Adapter is the proven LanguageLearner path; a per-character LoRA would give stronger keyframe fidelity at the cost of training setup. Benchmark LoRA vs IP-Adapter at M1 and decide.
21. **Music/SFX beyond H3 joint audio** — OP/ED stingers and BGM underscore: a local music model, a royalty-free music library, or rely solely on H3's `non_diegetic_music` prompt + joint audio? (Linked to open question #6.)
22. **Disk & retention** — episodes are ~1–2 GB each; define retention for keyframes/per-shot intermediates after assembly (delete vs archive), archive codec (H.265), and a cleanup job so `assets/` doesn't grow unbounded.
23. **Watchtower alerting** — a catastrophic failure at 03:00 should notify immediately (toast/webhook), not wait for the morning report; define severity levels and channels.
24. **Multi-episode overlap** — the bus makes two episodes concurrently possible (e.g., EP N in production while EP N+1 is in story review); whether to allow this and how budget/lock semantics change.
25. **Quality tier per episode** — native 1344×768 vs 480/720 short-edge as a configurable knob for burst periods, or fixed per series?

---

## 23. Appendix — key external references

- ComfyUI-MiniMaxH3-Director (timeline editor, Retake Stitch, Enhance Prompt, Preview Override, prompt format): https://github.com/seesee75-commits/ComfyUI-MiniMaxH3-Director
- MiniMax H3 model card (limits, prompt-writing guide): https://huggingface.co/Comfy-Org/MiniMax-H3
- LTX Director (the editor this ports): https://github.com/WhatDreamsCost/WhatDreamsCost-ComfyUI
- ComfyUI API (`/prompt`, `/queue`, `/history`, ws): https://docs.comfy.org
- **Source project (this design's character-consistency blueprint):** `C:\Users\Chad\PycharmProjects\LanguageLearner` — Krea 2 (`krea2TurboNSFWAIO_v10`) + Qwen3-TTS (CustomVoice/VoiceDesign) + IP-Adapter ref banks + structured prompt components + `gpu_manager.py` + `daily_generate.py`. Learned from it: §10.1, §12, §14.5, §15.4.
- Qwen3-TTS (12Hz 1.7B, CustomVoice + VoiceDesign): https://github.com/QwenLM/Qwen3-TTS

---

## 24. Walkthrough — blank state to first complete episode

Convention: `EVENT` = bus event (§6.4/§9.5), `HUMAN` = a human action, `[GPU:x]` = GPU-Manager acquisition/release on node x (§15.4), `→` = next event emitted. Node B = worker (orchestrator/LLM/TTS/Krea 2/assembly), node A = renderer (H3). Default approval modes apply (§9.5).

### Phase 0 — Machine bring-up (one-time, automated + config)

1. Install services: Redis broker (B), LM Studio (B), Qwen3-TTS env (B), ComfyUI ×2 (B=Krea 2, A=H3), ffmpeg. Mount SMB share on both nodes.
2. Deploy code; write `config/pipeline.yaml`, `bus.yaml`, `comfy.yaml`, `llm.yaml`, `qc.yaml`, `approval.yaml`; set `image.checkpoint = krea2TurboNSFWAIO_v10.safetensors`, H3 model paths (§10.3).
3. `studio.py verify` — health-checks bus, both ComfyUI instances, LM Studio, TTS, ffmpeg; runs an `emit → consume → ack` round-trip on `bus:system`.
4. Start `studio.py serve` on B (hosts timer source + stage consumers) and `studio.py renderer` on A. Each node publishes `NodeUp`. **Done: the studio is alive but has no show.**

### Phase 1 — `init-show`: iterative bootstrap → **Gate 0 sub-approvals (HUMAN approves each step)**

The wizard walks Concept → Bible → Character by character (refs + voice per character) → Scenes. Each step is approved or rejected-with-notes before the next generates. If `approval.global.auto_approve` is on, every step below auto-approves and lands in the dashboard for post-hoc review.

1. `studio.py init-show --name neon-sutra --brief "noir cyberpunk romance about a courier"` (or `--generate`, zero input) scaffolds `shows/neon-sutra/` + its `continuity/state.json` and starts the chain. Showrunner `[GPU:B→LLM]`.
2. **Concept** → `ConceptPending` → dashboard card: logline, genre, tone, maturity. **HUMAN** approves → `ConceptApproved` (reject → notes → regenerate). Concept is locked.
3. **Bible** → `BiblePending` → card: title, world/rules, arcs+beats, `style_guide`, runtime, episodes_per_arc, **and the `mature_spec`** (the show's promised maturity quotient, per-episode service-scene quotas, escalation schedule, tone boundaries — what the fan_service reviewer will enforce). **HUMAN** approves → `BibleApproved`. Locked.
4. **Character 1** (the AI proposes the roster and processes it in order):
   - `CharacterProposalPending{char: kiyo}` → card: name, `appearance_canonical`, personality, `h3_slot`, voice mode + `voice_description`. **HUMAN** approves → `CharacterProposalApproved{kiyo}` (reject → notes → regenerate that character only).
   - Auto-continues to **Gate 1**: `RefBankRequested{kiyo}` → Krea 2 renders 3–6 reference-sheet refs (neutral pose, gray bg) → `CharacterRefsPending{kiyo}` → dashboard contact sheet → **HUMAN** approves → `CharacterRefsApproved` (checksummed + locked as identity ground-truth).
   - Auto-continues to **Gate 2**: `VoiceSampleRequested{kiyo_jp}` → Qwen3-TTS VoiceDesign synthesizes audition lines → `VoiceSamplePending` → dashboard plays them → **HUMAN** approves → `VoiceApproved` (`voice_fingerprint` locked, audio cached).
   - The wizard then proposes **Character 2** … and so on until the roster is complete. Each character's refs+voice are bootstrapped the moment their proposal is approved, so the chain pipelines: while the user reviews Character 3's proposal, Character 2's refs are already rendering.
5. **Scenes** → `SceneRegistryPending` → card: initial locations + descriptions. **HUMAN** approves → `SceneRegistryApproved` → **bootstrap complete**; `continuity.state` initialized; episode 1 can be scheduled.
6. Every artifact is content-policy scanned (judge) before it reaches the user, and everything is **fictional-only** by directive.

**Bootstrap summary:** with `bootstrap.depth: full` this is ~N+4 approvals (Concept, Bible, Scenes, plus per character: proposal, refs, voice). `coarse` batches the cast. `auto` (or the master toggle) collapses everything to clicks-in-the-morning.

### Phase 2 — (folded into Phase 1; per-character refs + voices happen immediately after each character's proposal is approved)

### Phase 3 — (folded into Phase 1; ditto)

**The show is cast with zero authored content** — every word was AI-generated and steered only by approvals/rejections. No image, video, or line can now render a character inconsistently by accident.

### Phase 4 — First nightly run (02:00)

1. Timer source publishes `RunRequested{episode: 1, mode: overnight, budget_min: 480}`.
2. Run controller acquires `studio.lock` (dedup on `event_id`), opens DB, sees `continuity.episode = 0` and no in-flight run → creates `runs/EP01/`, row `run_1` `status=booting`.
3. Emits `RunStarted{run_id, episode: 1}`.

### Phase 5 — Development (Stage 1, Showrunner LLM)

1. Dev consumer `[GPU:B→LLM]` builds the system prompt (bible + continuity state + character sheets + policy + fictional-only + JSON contract), calls the Showrunner (`dolphin-2.9.3`-class, temp 0.8).
2. Returns `plan.json` — synopsis, beats covered, scenes_planned, estimated_shots, hook for next episode. Written atomically; DB commit.
3. Emits `PlanReady{run_id, plan_ref}`.
4. Story gate is **`auto` by default** → proceeds without blocking. The morning dashboard will show the plan for post-hoc review; a **HUMAN** reject later → `PlanRejected` → regenerate next run. (If `approval.yaml story: gated`, the run now shows `blocked_on: plan` and pauses here until `PlanApproved`.)
5. `[GPU:B]` releases LLM. Emits `ScriptRequested`.

### Phase 6 — Script + writers'-room review (Stage 2 → 2a, Showrunner + 2 reviewers)

1. Script consumer `[GPU:B→LLM]` — call #2. Emits `script.r1.json` (Draft r1): scenes (locations from the **scene registry** — empty at EP01, so new locations are proposed), per-shot entries with grid-snapped `duration_s`, `camera`, `action`, `dialogue[{char,line,start_s,end_s,on_camera}]`, `references`, `importance: hero|standard`.
2. **Costume/setting state (§14.5):** the same call runs the "keep/change outfit" decision → Kiyo's `outfit_state` for EP01 written to the character sheet + script (e.g. variant A all episode).
3. **Stage 2a review loop:** `ScriptReviewRequested{round:1}` → the **slop**, **continuity**, and **fan_service** reviewers run in parallel `[GPU:B→LLM]`, each writing its own notes file (`reviews/slop.r1.json`, `reviews/continuity.r1.json`, `reviews/fanservice.r1.json` — the latter checking the episode hits the `mature_spec` quotas from the approved bible) → notes found → `ScriptRevisionRequested` → Showrunner revises → `script.r2.json` → re-review → loop until all pass or `max_revisions` (default 2). Residual notes are flagged, never hidden.
4. Writes final `script.rN.json` + `script_reviews` rows; DB commit. Emits `ScriptReady` **with all reviewers' separate notes attached**.
5. Story gate (`auto`): proceeds; post-hoc human review available with the notes side by side. Emits nothing blocking.

### Phase 7 — Validate + plan shots (Stage 4, no GPU)

1. Validator consumer snaps durations to the 17k+5 grid (§10.2), checks ref counts ≤ H3 limits, dialogue fits (≥0.4 s slack), resolves character/scene/style refs to real files, and runs the **content-policy judge** over every prompt artifact (verdicts → `policy_events`).
2. Auto-fixes (duration snap) or hard-fails with a report.
3. Emits `ShotListReady{validated}` → run controller emits `ShotScheduled{shot_id}` × N (order + importance preserved), one per shot.

### Phase 8 — Producer-consumer production (Stages 3, 5, 6 interleaved)

Now node B feeds node A. For a 132-shot episode this is where most of the night goes (see §15).

1. **Keyframes (Stage 3, node B):** `[GPU:B→Krea2]`. For shot *N*: builds `keyframe_prompts` from `appearance_canonical` + `outfit_state` + location + camera + action + `style_guide`; character shots use `image_keyframe_ipadapter.json` (Kiyo's ref through IP-Adapter, weight 0.6, `end_at 0.6`), establishing shots use `image_keyframe.json`; the **mature-mode adult-descriptor guard** is applied at prompt compile. Emits `KeyframesReady{shot_id}` per shot.
2. **Dialogue (Stage 6, node B):** `[GPU:B→TTS]` resolves Kiyo's VoiceActor (`mode: designed`), synthesizes each line → per-shot dialogue track, emits `DialogueReady{shot_id, asset_ref}`.
3. **Shot render (Stage 5, node A):** renderer agent `[GPU:A→H3]` consumes `ShotScheduled{shot_id}`, waits for its `KeyframesReady` + `DialogueReady` refs, builds `h3_shot.json` (Refs ON: `@char1` = Kiyo ref, dialogue audio on the Audio track; hero shots get a second seed for best-of-2), submits to H3 via `POST /prompt`, watches the WebSocket, pulls the MP4 from `ComfyUI/output/`. Emits `ShotRendered{shot_id, asset_ref}` or `ShotFailed{reason}`.
4. Because shot *N+k*'s keyframes/dialogue are produced on B **while** A renders shot *N*, A never idles.
5. **Per-shot QC (Stage 8, node B)** on each `ShotRendered`:
   - Layer 0: duration, grey/black/NaN, freeze frames, silent audio, **anchor adherence** (SSIM vs the keyframes we fed), boundary continuity.
   - Layer 1: 3–5 sampled frames + dialogue peaks → `describer` judge → rubric (prompt_adherence, **identity vs Kiyo's ref bank**, lip_sync, artifacts, style, composite).
   - Hard fail (identity < 0.4, prompt < 0.4, or L0) → `ShotQcFailed{reason}` → `RetakeRequested` → `ShotScheduled` (new seed; budget-gated, ≤ 2/shot). Pass → `ShotQcPassed`; identity-clean crisp frames become **ref-bank promotion candidates (§14.6)**.
   - Borderline/hero shots also emit `ShotPendingApproval` → **GATE 4** (shot gate, `auto` default → treated as approved; human can later reject/retake from the dashboard).
6. **BudgetGate** watches `RunProgress`; on `BudgetExhausted` the run controller emits `RunAborted{paused: true}` — unrendered shots stay `ShotScheduled` and resume on the next `RunRequested`.

### Phase 9 — Assembly (Stage 7, node B)

1. When all shots are terminal (`ShotQcPassed` or `ShotSkipped`), the assembly consumer emits/consumes `AssemblyRequested`, orders shots by `shot_idx`, runs ffmpeg (concat + 1-frame crossfades, OP/ED cards, loudnorm; `ffmpeg_mux` dialogue overlaid where the shot's `audio_plan` said so).
2. Emits `AssemblyReady` → `runs/EP01/out/episode_preview.mp4`.

### Phase 10 — Whole-cut QC + **Gate 5: Episode (HUMAN)**

1. QC consumer runs the cut checks (sequence order, non-silent audio, runtime ±3 s) + optional Layer 2 continuity judge.
2. Emits `EpisodePendingApproval` → notifier toasts: **"EP01 is assembled and ready for review."** → dashboard Episode player.
3. **HUMAN** watches the cut → approves → `EpisodeApproved` → **air**. (Reject → rework queue: flagged shots → `RetakeRequested`, or full re-run.)

### Phase 11 — Delivery & continuity (Stage 9, node B)

1. Delivery consumer moves the cut to `archive/EP01/`, updates `feed.json`, generates an episode card (Krea 2).
2. **Ref-bank promotion (§14.6):** QC-verified in-show stills of Kiyo are added to `ref_images` (≤9 cap, evict weakest, seed sheet kept).
3. Showrunner produces `continuity_delta` (world facts changed, character status, plotlines, unresolved threads, `outfit_state`); run controller applies it to the show's `continuity/state.json` atomically and bumps `episode` → 1.
4. Writes `run_report.json`; emits `RunCompleted` + `ContinuityUpdated`.
5. Notifier toast: **"EP01 delivered — 132 shots, 3 retakes, 21:48."**

### Phase 12 — Morning review (HUMAN)

- Dashboard Episode wall shows EP01 (aired). Story was reviewable post-hoc; any rejected shots sit in the rework queue and flow back via `RetakeRequested` on the next run. `continuity.state` is ready to feed EP02 that night.

**First episode from blank state ≈ 1 setup session (phases 1–3, the iterative bootstrap wizard) + 2–3 nights of runs** (Phase 4–11), with **zero human-authored creative content**. Human approvals on the critical path depend on `bootstrap.depth`/`approval.global.auto_approve`: **`full`** ≈ N+4 approvals (Concept, Bible, each character's proposal+refs+voice, Scenes) plus the Episode air gate; **`auto`** = zero — everything lands in the dashboard for morning review. The user's only inputs are a one-line brief (or nothing) and clicks.
