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
            "script": "generate_script",
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
                if str(self.role.skill) == "show-bootstrap":
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
            if str(self.role.skill) == "script-reviewer":
                review_path = ep_dir / "reviews" / "script-review-r1.json"
                scripts = sorted(ep_dir.glob("script.r*.json"))
                scene_files = (sorted((ep_dir / "script_scenes").glob("*.json"))
                               if (ep_dir / "script_scenes").exists() else [])
                task = (
                    "TASK: Review the latest screenplay against the skill. Read "
                    "these files with the read tool:\n"
                    f"- plan: {plan_path}\n"
                    f"- latest script: {scripts[-1] if scripts else '(none)'}\n"
                    "- scene files:\n"
                    + ("\n".join(f"  {p}" for p in scene_files) or "  (none)")
                    + f"\n- canon: {show_dir / 'bible.yaml'}, "
                    f"{show_dir / 'characters'}, "
                    f"{show_dir / 'continuity' / 'state.json'}\n"
                    f"Write this file with the write tool:\n  {review_path}\n"
                    "Format: {\"round\": 1, \"passed\": bool, "
                    "\"reviews\": {name: {\"grade\": \"Pass\"|\"Fail\", "
                    "\"pass\": bool, \"review\": \"...\"}}}.\n"
                    "Blocking reviewers: structure, continuity, fan_service. "
                    "Use one short sentence per reviewer.\n"
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
                    "and scenes. Keep approved artifacts that are not affected. "
                    "All characters are adults (18+).\n"
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
                "few lines in character. All characters are adults (18+).\n"
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
                "All characters are adults (18+).\n"
            )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        elif str(self.role.skill) == "object-prompts":
            apply_obj_py = root / "tools" / "apply_object_prompts.py"
            obj_path = ep_dir / "object-prompts.json"
            task = (
                f"TASK: Write the episode's object image prompts to this file with "
                f"the write tool:\n  {obj_path}\n"
                "Format: {\"objects\": [{\"name\": \"exact plan object name\", "
                "\"description\": \"one paragraph of visual detail\"}]}.\n"
                "Use the plan's objects list; include every object. Describe the "
                "object's shape, materials, scale, markings and wear, not a scene "
                "or a character.\n"
                f"Then run (bash): python \"{apply_obj_py}\" --card "
                f"{self.card.get('id')}\n"
                "It builds and saves the Krea 2 object prompts. Fix missing "
                "objects and re-run.\n"
                "Do not read, glob, or grep the show; the plan, canon, and skill "
                "are in this prompt.\n"
            )
            finish = (f'python "{studio_py}" board-finish --run {{run}} '
                      '--status verified|retake|blocked --summary "..."')
        elif str(self.role.skill) == "script-writer":
            assemble_py = root / "tools" / "assemble_script.py"
            scenes_dir = ep_dir / "script_scenes"
            existing = (sorted(p.stem for p in scenes_dir.glob("*.json"))
                        if scenes_dir.exists() else [])
            notes = (self.card.get("notes")
                     or (self.card.get("payload") or {}).get("notes") or [])
            task = (
                "TASK: Write the screenplay scene files for every plan scene, in "
                f"plan order, under:\n  {scenes_dir}\\<scene id>.json\n"
                f"Scene files already present: {', '.join(existing) or 'none'}.\n"
                "For each plan scene, write a JSON object with id, location, "
                "time_of_day, summary, narrative, characters, and a shots list of "
                "4-8 shots. Each shot needs id, type, importance, duration_s, "
                "beat, camera, action, references, soundscape, music, dialogue, "
                "and silence. Use the SKILL schema exactly.\n"
                "If the card has notes, rewrite every affected scene; leave "
                "unaffected existing scene files alone. Do not use filler.\n"
                "After every plan scene has a file, run (bash): python "
                f"\"{assemble_py}\" --card {self.card.get('id')}\n"
                "It validates, normalizes, resolves locations, writes the next "
                "script.rN.json, and attaches it. Fix any missing/invalid scene "
                "and re-run.\n"
                "Do not read, glob, or grep the show; the plan, show canon, and "
                "skill are in this prompt. Do not write script.r*.json yourself.\n"
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
                    "2-3 distinct scenes, vary locations, and keep every "
                    "character an adult (18+). Do not write minors.\n"
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
            f"You work exactly one card. Phase: {self.phase}.\n\n"
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
            self.proc = subprocess.Popen(
                cmd, cwd=str(self.worker), stdin=subprocess.PIPE,
                stdout=log, stderr=err, env=env, text=True, creationflags=flags)
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
