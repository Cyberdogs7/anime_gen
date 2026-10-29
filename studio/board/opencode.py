"""Opencode worker: one `opencode run` per card, rooted at ``board/``.

No plugin, no attached server. The worker config in ``board/opencode.json``
allowlists exactly two shell commands (the board CLI bridge); everything else is
denied. The board remains the only writer and the GPU lease stays in Python.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import tools as tools_mod
from ..prompts import BOARD_AGE_GATE
from .agent import AgentResult


def worker_dir(cfg) -> Path:
    return Path(cfg.root) / "board"


def _opencode_exe() -> str | None:
    """Resolve the real binary, not the npm .cmd shim (shim children leak)."""
    found = shutil.which("opencode")
    if not found:
        return None
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat"):
        candidate = (path.parent / "node_modules" / "opencode-ai" / "bin"
                     / "opencode.exe")
        if candidate.exists():
            return str(candidate)
    return found


REMOTE_PREFIXES = ("opencode/", "opencode-go/", "openrouter/", "quota-router/")


# Fragment passes: skill -> (fragment suffix, one-line job, inputs note).
# The producer task branch below is parameterized from this table so every
# single-responsibility pass shares one prompt shape.
_FRAGMENT_WRITERS = {
    "location-writer": ("location",
        "location fragment (set + time of day)",
        "the plan's location fields and registry"),
    "cast-writer": ("cast",
        "cast fragment (exact on-screen names)",
        "the plan cast plus the location fragments"),
    "outfit-writer": ("outfits",
        "outfit fragment (costume label per character)",
        "the cast and location fragments plus the canon wardrobe"),
    "object-prompts": ("objects",
        "object fragment (props that matter, with visual detail)",
        "the plan objects plus the location and cast fragments"),
    "narrative-writer": ("narrative",
        "narrative fragment (2-5 sentences of what happens)",
        "the plan beats plus the location, cast, and object fragments"),
    "dialogue-writer": ("dialogue",
        "dialogue fragment (numbered spoken lines only)",
        "the narrative fragment plus prior fragments for voice"),
    "shot-splitter": ("shots",
        "shot fragment (boundaries, durations, line refs — no camera)",
        "the narrative and dialogue fragments"),
    "camera-director": ("camera",
        "camera fragment (framing/angle/movement per shot)",
        "the shots fragment plus the n-1/n+1 shots for continuity"),
    "action-writer": ("action",
        "action fragment (pose-to-pose movement per shot)",
        "the shots and camera fragments"),
    "sound-writer": ("sound",
        "sound fragment (soundscape + music cue per shot)",
        "the shots fragment"),
}


def _needs_preload(model: str) -> bool:
    return not str(model or "").startswith(REMOTE_PREFIXES)


def _local_model(model: str) -> str:
    m = str(model or "")
    return m[len("lm_studio_local/"):] if m.startswith("lm_studio_local/") else m


def _model_id(cfg, role) -> str:
    provider = str((cfg.get("agents", "opencode", {}) or {}).get("provider")
                   or "lm_studio_local")
    model = str(role.model or "")
    if any(model.startswith(prefix) for prefix in REMOTE_PREFIXES):
        return model
    # Local LM Studio ids may themselves contain a slash (e.g. qwen/qwen3-vl-8b),
    # so the provider prefix is required even then.
    return f"{provider}/{model}" if model else provider


class OpenCodeAgent:
    def __init__(self, *, role, card: dict[str, Any], store, phase: str,
                 run_id: str, cfg, llm=None):
        self.role = role
        self.card = card
        self.store = store
        self.phase = phase
        self.run_id = run_id
        self.cfg = cfg
        self.llm = llm
        self.worker = worker_dir(cfg)
        self.run_dir = self.worker / "runs" / run_id
        self.proc: subprocess.Popen | None = None

    def system_prompt(self) -> str:
        if getattr(self.role, "skill", ""):
            return self._skill_prompt()
        job = self.role.prompt or "Do this card's work."
        kind = str(self.card.get("kind") or "")
        produce_tool = {
            "outline": "outline_merge",
            "outline_skeleton": "outline_skeleton",
            "outline_beats": "outline_beats",
            "outline_scene": "rewrite_scene",
        }.get(kind)
        if (self.phase == "produce" and produce_tool
                and not getattr(self.role, "iterate", False)):
            job = (f"Call {produce_tool} exactly once, then finish verified. "
                   "The tool reads the show canon and this card (including any "
                   "reviewer notes on it); pass no flags unless a note says to. "
                   "Do not call read_show, inspect files, read old runs, or run "
                   "any other discovery tool.")
        card = {k: self.card.get(k) for k in
                ("id", "kind", "title", "show_id", "episode", "state", "payload",
                 "notes")}
        if self.phase == "review":
            finish = ('python ../studio.py board-finish --run {run} '
                      '--verdict pass|fail')
        else:
            finish = ('python ../studio.py board-finish --run {run} '
                      '--status verified|retake|blocked --summary "..."')
        finish = finish.format(run=self.run_id)
        iterate = bool(getattr(self.role, "iterate", False))
        guard = (
            "IMPORTANT: you have exactly ONE tool: bash. Never call add_note, "
            "outline_merge, run_outline_review, board-finish or any board tool "
            "by name. Every action, including finish, must be a bash command.\n"
        ) if iterate else ""
        start = ("Choose the next board CLI command for the card's kind.\n"
                 if iterate else "Start with the tool named above.\n")
        return (
            f"You are the {self.role.name} agent on an anime production board.\n"
            f"You work exactly one card. Phase: {self.phase}.\n"
            f"Central gate — applies to everything you do on this card:\n"
            f"{BOARD_AGE_GATE}\n\n"
            f"{job}\n\n"
            f"{guard}"
            f"CARD:\n{self._json(card)}\n\n"
            f"RUN: {self.run_id}\nCARD_ID: {self.card.get('id')}\n\n"
            "Do the work only through the board CLI. Put --run and --card first, "
            "then the tool's flags:\n"
            f"  python ../studio.py board-tool --name <tool> --run {self.run_id} "
            f"--card {self.card.get('id')} <flags>\n"
            "Tools marked [long] take minutes: add --async, read the job_id, then "
            "repeat board-wait until state is done or failed:\n"
            f"  python ../studio.py board-tool --async --name <tool> --run {self.run_id} "
            f"--card {self.card.get('id')} <flags>\n"
            "  python ../studio.py board-wait --job <job_id> --timeout 110\n"
            "job_id is the value in the board-tool --async output, not the run id. "
            "Do not submit the same long tool twice while its job is running.\n"
            "The board tools are the only source of canon. Never inspect files, "
            "directories, run logs, old traces, or config; never use read, glob, "
            f"grep, or list. {start}"
            "Board CLI tools (invoke through bash; never call these names directly):\n"
            f"{tools_mod.manifest(self.role)}\n\n"
            f"Finish with (also a bash command):\n  {finish}\n"
            "Use one short command per step. Do not edit files."
        )

    def _skill_prompt(self) -> str:
        root = self.cfg.root
        skill_path = root / ".agents" / "skills" / str(self.role.skill) / "SKILL.md"
        show_dir = root / "shows" / str(self.card.get("show_id") or "")
        episode = int(self.card.get("episode") or 0)
        ep_dir = show_dir / "runs" / f"EP{episode:02d}"
        plan_path = ep_dir / "plan.json"
        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except OSError:
            skill_text = f"(skill missing at {skill_path})"
        plan_exists = plan_path.exists()
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except OSError:
            plan_text = "(plan.json does not exist yet)"
        show_context = "(show context not loaded)"
        if self.phase != "review":
            try:
                from ..show import Show
                from ..season import read_season_entry
                from ..scenes import registry_block
                from ..planner import read_director_notes
                show = Show(str(self.card.get("show_id") or ""))
                if str(self.role.skill) in ("show-bootstrap", "show-ideas"):
                    import yaml as _yaml
                    scenes = []
                    for sid in show.list_scenes():
                        try:
                            scenes.append(_yaml.safe_load(
                                (show.scenes_dir / f"{sid}.yaml").read_text(
                                    encoding="utf-8")) or {})
                        except Exception:
                            continue
                    show_context = self._json({
                        "profile": show.style_profile(self.cfg),
                        "concept": show.read_concept(),
                        "bible": show.read_bible(),
                        "characters": [show.read_character(cid)
                                       for cid in show.list_characters()],
                        "scenes": scenes,
                        "ideas": show.read_ideas(),
                        "bootstrap": show.bootstrap_state(),
                    })
                else:
                    season_no, season_entry = read_season_entry(show, episode)
                    show_context = self._json({
                        "bible": show.read_bible(),
                        "characters": [show.read_character(cid)
                                       for cid in show.list_characters()],
                        "continuity": show.read_continuity(),
                        "season": {"number": season_no, "entry": season_entry},
                        "scene_registry": registry_block(show),
                        "director_notes": read_director_notes(show),
                    })
            except Exception as exc:
                show_context = f"(show context unavailable: {exc})"
        card = {k: self.card.get(k) for k in
                ("id", "kind", "title", "show_id", "episode", "state", "payload",
                 "notes")}
        studio_py = root / "studio.py"
        record_py = root / "tools" / "record_outline_review.py"
        apply_py = root / "tools" / "apply_outline_changes.py"
        if self.phase == "review":
            if str(self.role.skill) != "outline-reviewer":
                # Fragment reviewers (and any future skill reviewer): the
                # skill names its own read/write files, so the task stays
                # generic. The outline reviewer below keeps its record step.
                task = (
                    "TASK: Review per the SKILL above. Use the read tool for "
                    "the files it names under:\n"
                    f"  {ep_dir}\n"
                    "Write the review file the skill names with the write "
                    "tool. Do not read, glob, or grep the show beyond what "
                    "the skill allows; do not edit the reviewed files.\n"
                )
                finish = (f'python "{studio_py}" board-finish --run {{run}} '
                          '--verdict pass|fail --notes "..."')
            else:
                review_path = ep_dir / "reviews" / "outline-review-r1.json"
                task = (
                    f"TASK: Review the plan against the skill. Write this file with "
                    f"the write tool:\n  {review_path}\n"
                    "Format: {\"round\": 1, \"passed\": bool, "
                    "\"reviews\": {name: {\"grade\": \"Pass\"|\"Fail\", "
                    "\"pass\": bool, \"review\": \"...\"}}}.\n"
                    f"Then run (bash): python \"{record_py}\" "
                    f"--plan \"{plan_path}\" --review \"{review_path}\"\n"
                    "Do not read, glob, or grep the show; the plan and skill are in "
                    "this prompt.\n"
                )
                finish = (f'python "{studio_py}" board-finish --run {{run}} '
                          '--verdict pass|fail --notes "..."')
        elif str(self.role.skill) == "show-bootstrap":
            apply_boot_py = root / "tools" / "apply_show_bootstrap.py"
            payload = self.card.get("payload") or {}
            step = str(payload.get("step") or "").strip()
            notes = str(payload.get("notes") or "").strip()
            if not notes:
                notes = "\n".join(
                    str(n.get("text") if isinstance(n, dict) else n)
                    for n in (self.card.get("notes") or [])).strip()
            paths = (
                f"- concept: {show_dir / 'concept.json'}\n"
                f"- bible: {show_dir / 'bible.yaml'}\n"
                f"- characters: {show_dir / 'characters'}/<id>.yaml\n"
                f"- scenes: {show_dir / 'scenes'}/<id>.yaml\n"
            )
            if step:
                task = (
                    f"TASK: Revise the show's {step} artifact from the notes.\n"
                    f"NOTES:\n{notes or '(none)'}\n"
                    f"Artifact paths:\n{paths}"
                    "Use the current artifacts in SHOW CANON. Rewrite only the "
                    "affected artifact; a concept change also refreshes bible, "
                    "characters, and scenes; a bible change refreshes characters "
                    "and scenes. Keep approved artifacts that are not affected.\n"
                    f"Then run (bash): python \"{apply_boot_py}\" --card "
                    f"{self.card.get('id')}\n"
                    "It validates, updates bootstrap state, and attaches the "
                    "artifacts. Fix invalid files and re-run.\n"
                )
            else:
                task = (
                    "TASK: Build the new show's text artifacts from the owner's "
                    f"brief and the show profile.\nArtifact paths:\n{paths}"
                    "Write concept.json and bible.yaml, one character YAML per "
                    "cast member (3-5 total), and 5-8 location YAMLs. Use the "
                    "SKILL schemas exactly."
                    "Use the profile's maturity as the "
                    "content policy and the brief's premise as canon.\n"
                    f"Then run (bash): python \"{apply_boot_py}\" --card "
                    f"{self.card.get('id')}\n"
                    "It validates, writes bootstrap state, and attaches the "
                    "artifacts. Fix invalid/missing files and re-run.\n"
                )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        elif str(self.role.skill) == "show-ideas":
            check_py = root / "tools" / "check_show_ideas.py"
            draft_py = root / "tools" / "draft_show_ideas.py"
            ideas_path = show_dir / "ideas.json"
            brief = str((self.card.get("payload") or {}).get("brief") or "")
            n = int((self.card.get("payload") or {}).get("n") or 5)
            task = (
                "TASK: Pitch 5 distinct executions of the owner's brief.\n"
                "Step 1 (do this FIRST, before anything else): run (bash):\n"
                f"  python \"{draft_py}\" --card {self.card.get('id')}\n"
                "It makes the draft LLM call and writes the archive file:\n"
                f"  {ideas_path}\n"
                "Only python helper commands are allowed in bash; type/cat/read "
                "are denied. A missing ideas.json is EXPECTED before step 1 — "
                "run the draft command instead of inspecting anything.\n"
                f"Owner brief:\n{brief or '(none)'}\n"
                f"Then run (bash): python \"{check_py}\" --show "
                f"{self.card.get('show_id')}\n"
                "It names the pitches to rewrite (bad schema/slug/duplicates). "
                "Rewrite ONLY flagged pitches with the edit tool, keep the good "
                "ones, and re-run the checker (max 4 rounds). Then do one critic "
                "pass: if two pitches feel like the same show, or one ignores "
                "the brief, rewrite the weaker and re-run the checker. "
                "Do not write pitches in chat; the archive file is the only "
                "draft surface.\n"
                "Copy the board-finish command EXACTLY as given below; its "
                "script is <root>\\studio.py, never tools\\studio.py.\n"
            )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        elif str(self.role.skill) == "voice-writer":
            import re as _re
            char = str((self.card.get("payload") or {}).get("char") or "").strip()
            slug = _re.sub(r"[^a-z0-9-]+", "-", char.lower()).strip("-")
            sample_path = show_dir / "voices" / f"{slug}.txt"
            apply_voice_py = root / "tools" / "apply_voice_sample.py"
            task = (
                f"TASK: Write a 150-300 word first-person voice sample for {char} "
                f"to this file with the write tool:\n  {sample_path}\n"
                "Plain text only: read-aloud rhythm, attitude, vocabulary, and a "
                "few lines in character.\n"
                f"Then run (bash): python \"{apply_voice_py}\" --card "
                f"{self.card.get('id')}\n"
                "It saves the sample, resets the voice state, and creates the TTS "
                "card. Fix a short/invalid sample and re-run.\n"
                "Do not read, glob, or grep the show; the canon and skill are in "
                "this prompt.\n"
            )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        elif str(self.role.skill) == "qc-fixer":
            assemble_py = root / "tools" / "assemble_script.py"
            requeue_py = root / "tools" / "requeue_qc_fixes.py"
            failures = (self.card.get("payload") or {}).get("failures") or []
            shot_ids = [str(f.get("shot_id") or "") for f in failures
                        if isinstance(f, dict) and f.get("shot_id")]
            scenes_dir = ep_dir / "script_scenes"
            task = (
                "TASK: Fix the failed shots, then requeue them. Read the scene "
                f"files under:\n  {scenes_dir}\n"
                f"Failed shot ids: {', '.join(shot_ids) or '(none)'}\n"
                "Read each affected scene file, revise only the failed shots' "
                "action/camera/references/dialogue/soundscape to fix the reported "
                "QC/VLM issue, keep every other shot and the story beat intact, "
                "and write the full scene file back.\n"
                f"Then run (bash): python \"{assemble_py}\" --card "
                f"{self.card.get('show_id')}.ep{episode:02d}.script\n"
                f"Then run (bash): python \"{requeue_py}\" --card "
                f"{self.card.get('id')}\n"
                "The assembler writes the new script revision; the requeue helper "
                "creates the render cards. Do not use apply_qc_fixes.\n"
            )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        elif str(self.role.skill) in _FRAGMENT_WRITERS:
            frag, what, inputs = _FRAGMENT_WRITERS[str(self.role.skill)]
            passes_dir = ep_dir / "passes"
            hash_py = root / "tools" / "pass_hash.py"
            check_py = root / "tools" / "check_pass.py"
            prev_hash = str((self.card.get("result") or {}).get("input_hash") or "")
            task = (
                f"TASK: Write the {what} for every plan scene, in plan order, "
                f"one file per scene with the write tool:\n  {passes_dir}"
                f"\\<scene id>.{frag}.json\n"
                f"Inputs: the CURRENT PLAN above plus {inputs}. Write exactly "
                "one concern per file — the SKILL defines the schema; do not "
                "add other passes' fields.\n"
                "Step 0 (fast-pass): run (bash): python "
                f"\"{hash_py}\" --card {self.card.get('id')}\n"
                "It prints one sha1 over this pass's inputs. If it matches "
                f"input_hash below AND every scene file exists, the inputs are "
                "unchanged: do not rewrite anything, finish --status verified "
                "with a one-line summary.\n"
                f"input_hash: {prev_hash or '(none yet)'}\n"
                "If the card has notes, rewrite every affected scene; leave "
                "unaffected existing files alone.\n"
                f"Then run (bash): python \"{check_py}\" --card "
                f"{self.card.get('id')} --pass {frag}\n"
                "It validates every fragment (schema + cross-refs). Fix "
                "flagged files and re-run.\n"
                "Do not read, glob, or grep the show; the plan, needed prior "
                "fragments, skill, and card are in this prompt.\n"
            )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        elif str(self.role.skill) == "h3-writer":
            merge_py = root / "tools" / "merge_passes.py"
            compile_py = root / "tools" / "compile_h3_prompts.py"
            assemble_py = root / "tools" / "assemble_script.py"
            task = (
                "TASK: Merge the episode fragments into scene files, compile "
                "the H3 prompts, then assemble the script. Run (bash):\n"
                f"  python \"{merge_py}\" --card {self.card.get('id')}\n"
                "It merges runs/EPxx/passes/<sid>.*.json into script_scenes/ "
                "<sid>.json (full scene schema), builds object-prompts.json, "
                "and saves the Krea object prompts. Fix flagged fragments "
                "(edit ONLY the named pass files) and re-run.\n"
                "  python "
                f"\"{compile_py}\" --card {self.card.get('id')}\n"
                "It compiles every shot's H3 prompt without rendering and "
                "reports shots whose framed characters/sets lack approved "
                "refs. If it reports gaps, do NOT invent refs: finish "
                "--status verified anyway (mechanical merge done) — the "
                "reviewer fails the card with directives for the right "
                "upstream pass.\n"
                "  python "
                f"\"{assemble_py}\" --card {self.card.get('id')}\n"
                "It validates, normalizes, writes the next script.rN.json, "
                "and attaches it.\n"
                "Do not read, glob, or grep the show; the card and skill are "
                "in this prompt. Do not write script.r*.json yourself.\n"
            )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        else:
            import json as _json
            try:
                _plan = _json.loads(plan_text)
                plan_ready = bool(_plan.get("scenes")) and all(
                    len(s.get("beats") or []) == 3 for s in _plan["scenes"])
            except Exception:
                plan_ready = False
            notes = (self.card.get("notes")
                     or (self.card.get("payload") or {}).get("notes") or [])
            changes_path = ep_dir / "outline-changes.json"
            if not plan_exists:
                task = (
                    "TASK: NO PLAN EXISTS. Create the full episode outline now. "
                    f"Write this file with the write tool:\n  {changes_path}\n"
                    "Required top-level fields: episode, title, episode_type "
                    "('character'|'battle'), threat_of_the_week, plot, synopsis, "
                    "characters, cast, objects, plotline_updates. Then 'scenes' "
                    "with 14-18 full scene objects for a 22-minute episode, and "
                    "'order' with every scene id. Each scene needs id, summary, "
                    "characters, location_id, location, location_setting, "
                    "time_of_day, and exactly 3 beats (Setup/Change/Consequence).\n"
                    "Use the SHOW CANON above: expand each season key beat into "
                    "2-3 distinct scenes and vary locations.\n"
                    "Use the write tool exactly once for that file; do not print "
                    "the JSON in chat and do not use bash to write it.\n"
                    f"Then run (bash): python \"{apply_py}\" --card "
                    f"{self.card.get('id')}\n"
                    "It creates plan.json and validates the scenes/order. Fix "
                    "the changes file and re-run if it reports an error.\n"
                    "Do not read, glob, or grep the show; the show canon, skill, "
                    "and card are in this prompt. Do not write plan.json "
                    "yourself.\n"
                )
            elif plan_ready and not notes:
                task = (
                    "TASK: The plan already exists with valid scenes and the "
                    "card has no notes. Do not write or change any file. Run the "
                    "finish command with --status verified and a one-line "
                    "summary of the plan.\n"
                )
            else:
                task = (
                    f"TASK: Apply the CARD notes by writing ONLY the changed parts "
                    f"with the write tool:\n  {changes_path}\n"
                "Format: {\"plot\"?: str, \"synopsis\"?: str, "
                "\"scenes\": [full changed scene objects], "
                "\"remove_scene_ids\"?: [ids], \"order\"?: [ids]}. "
                "Omit scenes you do not change.\n"
                "Use the write tool exactly once for that file; do not print "
                "the JSON in chat and do not use bash to write it.\n"
                f"Then run (bash): python \"{apply_py}\" --card "
                f"{self.card.get('id')}\n"
                "It finds the changes file and merges/validates plan.json; fix "
                "the changes file and re-run if it reports an error.\n"
                    "Do not read, glob, or grep the show; the plan, skill, and card "
                    "are in this prompt. Do not write plan.json yourself.\n"
                )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        finish = finish.format(run=self.run_id)
        show_block = (f"SHOW CANON:\n{show_context}\n\n"
                      if self.phase != "review" else "")
        return (
            f"You are the {self.role.name} agent on an anime production board.\n"
            f"You work exactly one card. Phase: {self.phase}.\n"
            f"Central gate — applies to everything you do on this card:\n"
            f"{BOARD_AGE_GATE}\n\n"
            f"SKILL:\n{skill_text}\n\n"
            f"{show_block}"
            f"CURRENT PLAN ({plan_path}):\n{plan_text}\n\n"
            f"CARD:\n{self._json(card)}\n\n"
            f"RUN: {self.run_id}\nCARD_ID: {self.card.get('id')}\n"
            f"SHOW: {self.card.get('show_id')}\nEPISODE: {episode}\n"
            f"SHOW_DIR: {show_dir}\nEPISODE_DIR: {ep_dir}\n\n"
            f"{task}\n"
            "Do not use board-tool or board-wait. Do not call the Python LLM job "
            "functions; you are the writer/reviewer. Do not commit or push.\n"
            f"Finish with (bash):\n  {finish}\n"
        )

    @staticmethod
    def _json(value: Any) -> str:
        import json
        return json.dumps(value, ensure_ascii=False)

    def run(self) -> AgentResult:
        from ..gpu_manager import hold_llm
        pin = (_local_model(self.role.model)
               if getattr(self.role, "pin_model", False) else None)
        with hold_llm(pin):
            return self._run()

    def _run(self) -> AgentResult:
        if _needs_preload(self.role.model):
            try:
                from ..gpu_manager import preload_model
                preload_model(self.cfg, _local_model(self.role.model))
            except Exception as exc:
                return AgentResult(status="blocked", error=f"model preload: {exc}")
        exe = _opencode_exe()
        if not exe:
            return AgentResult(status="blocked", error="opencode is not on PATH")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        prompt = self.system_prompt()
        (self.run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        env = os.environ.copy()
        env.pop("OPENCODE_SERVER_PASSWORD", None)
        env.pop("OPENCODE_SERVER_USERNAME", None)
        env.update({
            "STUDIO_BOARD_URL": str(self.cfg.get("agents", "board_url",
                                                 "http://127.0.0.1:8130")),
            "STUDIO_ROOT": str(self.cfg.root),
            "STUDIO_RUN_ID": self.run_id,
            "STUDIO_CARD_ID": str(self.card.get("id") or ""),
            "STUDIO_ROLE": self.role.name,
        })
        agent_name = str(getattr(self.role, "agent", "") or "studio")
        cmd = [exe, "run", "--model", _model_id(self.cfg, self.role),
               "--agent", agent_name, "--dir", str(self.worker),
               "--title", f"{self.role.name} {self.card.get('id', '')}"]
        log = open(self.run_dir / "stdout.log", "wb")
        err = open(self.run_dir / "stderr.log", "wb")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            # Explicit UTF-8: the prompt routinely contains non-ASCII
            # glyphs (opencode renders failed tool calls with ✗, model
            # output may too). The Windows locale default (cp1252) cannot
            # encode them and the spawn dies with "spawn failed: 'charmap'
            # codec can't encode...". Node reads stdin as UTF-8.
            self.proc = subprocess.Popen(
                cmd, cwd=str(self.worker), stdin=subprocess.PIPE,
                stdout=log, stderr=err, env=env, text=True,
                encoding="utf-8", creationflags=flags)
            if self.proc.stdin:
                self.proc.stdin.write(prompt)
                self.proc.stdin.close()
        except Exception as exc:
            log.close()
            err.close()
            return AgentResult(status="blocked", error=f"spawn failed: {exc}")
        try:
            return self._wait()
        finally:
            log.close()
            err.close()

    def _wait(self) -> AgentResult:
        timeout_s = float((self.card.get("payload") or {}).get("timeout_s")
                          or self.role.timeout_s or 1800)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            run = self.store.run(self.run_id) or {}
            final = (run.get("result") or {}).get("final")
            if final:
                self._stop()
                return self._to_result(final)
            if self.proc and self.proc.poll() is not None:
                tail = self._tail()
                self._cancel_jobs("worker exited without a verdict")
                # A worker that exits without calling board-finish is an
                # infrastructure failure (crash, model stall, OOM), not a
                # content verdict. Return retake so the daemon retries up to
                # max_attempts instead of dead-ending on the first failure.
                # After the cap, _requeue_retakes parks it as blocked with
                # "retake cap" and the tail stays in board_runs.error.
                return AgentResult(
                    status="retake",
                    error=f"opencode exited {self.proc.returncode} without a "
                          f"verdict. {tail}")
            time.sleep(0.5)
        self._stop()
        self._cancel_jobs("worker timed out")
        return AgentResult(status="retake", error="opencode run timed out")

    def _cancel_jobs(self, reason: str) -> None:
        """Stop long jobs this card started; the worker will not collect them."""
        try:
            from . import api as board_api
            board_api.cancel_jobs_for_card(str(self.card.get("id") or ""), reason)
        except Exception:
            pass

    def _stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass

    def _tail(self, limit: int = 1200) -> str:
        for name in ("stderr.log", "stdout.log"):
            try:
                text = (self.run_dir / name).read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    return text[-limit:]
            except OSError:
                pass
        return ""

    def _to_result(self, final: dict[str, Any]) -> AgentResult:
        notes = str(final.get("notes") or "").strip()
        if self.phase == "review":
            verdict = str(final.get("verdict") or "").strip().lower()
            if verdict not in ("pass", "fail"):
                return AgentResult(status="blocked",
                                   error="reviewer verdict must be pass or fail")
            return AgentResult(status="pass" if verdict == "pass" else "retake",
                               verdict=verdict, notes=notes)
        status = str(final.get("status") or "").strip().lower()
        if status not in ("verified", "retake", "blocked"):
            return AgentResult(status="blocked",
                               error="producer status must be verified/retake/blocked")
        return AgentResult(status=status,
                           summary=str(final.get("summary") or "").strip(),
                           notes=notes, error=str(final.get("error") or "").strip())
