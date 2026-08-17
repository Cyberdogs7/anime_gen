"""Bootstrap chain (Gate 0) - Concept -> Bible -> Characters (proposal/refs/voice) -> Scenes.

Drives the iterative bootstrap (DESIGN.md §9.5a) with the Showrunner LLM.
State lives in shows/<id>/bootstrap.json; approval events are published on the
bus for the audit log / future dashboard. Reject-with-notes regenerates only
the current step (approved steps stay locked).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import prompts
from .bus import make_broker
from .bus.events import (
    BIBLE_APPROVED, BIBLE_PENDING, BOOTSTRAP_COMPLETE, CHAR_PROPOSAL_APPROVED,
    CHAR_PROPOSAL_PENDING, CHAR_REFS_APPROVED, CHAR_REFS_PENDING, CONCEPT_APPROVED,
    CONCEPT_PENDING, SCENE_REGISTRY_APPROVED, SCENE_REGISTRY_PENDING, VOICE_APPROVED,
    VOICE_SAMPLE_PENDING, Event, new_event,
)
from .clients import LMStudioClient, TTSService
from .clients.tts import VoiceConfig
from .config import get_config
from .show import Show

log = logging.getLogger(__name__)

STEPS = ("concept", "bible", "characters", "scenes")

# Live progress for the dashboard: show_id -> {"detail": str, "ts": float}.
# Written by BootstrapChain._report while a synchronous approve/reject request
# is running; cleared by approval.py once the chain finishes.
ACTIVITY: dict[str, dict[str, Any]] = {}

# Per-show locks: the Gate-0 chain is resumable and self-healing, so
# approve/reject clicks and background reconcile runs must never interleave.
# RLock: run_show_locked() may wrap an advance() that itself re-acquires.
_show_locks: dict[str, threading.RLock] = {}
_show_locks_guard = threading.Lock()
_reconciling: set[str] = set()


def _show_lock(show_id: str) -> threading.RLock:
    with _show_locks_guard:
        lock = _show_locks.get(show_id)
        if lock is None:
            lock = threading.RLock()
            _show_locks[show_id] = lock
        return lock


def run_show_locked(show_id: str, fn) -> Any:
    """Run fn under the show's Gate-0 lock (blocks until free)."""
    with _show_lock(show_id):
        return fn()


def _missing_scene_refs(show: Show) -> bool:
    """True when any scene in the registry still lacks its location ref image.

    Scene refs are non-gating asset work (they must land for the dashboard to
    show locations while the registry awaits approval). A stub marker counts as
    done so a missing krea2 instance can't cause an infinite retry loop.
    """
    for sid in show.list_scenes():
        rd = show.scenes_dir / sid / "refs"
        if (rd / f"{sid}_ref_01.png").exists():
            continue
        if (rd / "refs.json").exists():
            continue
        return True
    return False


def needs_reconcile(show_id: str, show: Show | None = None) -> bool:
    """True when the show is incomplete but NOT waiting on a human gate.

    Self-healing signal: if a previous advance() was interrupted (crash /
    dashboard restart) it left the chain mid-work with no pending artifact.
    This detects that stuck state so a caller can resume generation.
    """
    show = show or Show(show_id)
    st = show.bootstrap_state()
    if st.get("complete"):
        return False
    if (st.get("concept") or {}).get("status") == "pending":
        return False
    if (st.get("bible") or {}).get("status") == "pending":
        return False
    for ch in st.get("characters", []):
        if ch.get("proposal") == "pending" or ch.get("voice") == "pending":
            return False
        # Refs gate independently: a generated ref ("real") awaits human
        # approval before the chain can advance past it.
        if ch.get("refs") not in ("approved", "", None):
            return False
    if (st.get("scenes") or {}).get("status") == "pending":
        # The registry itself awaits the human, but its location ref images are
        # non-gating asset work — if any are missing, resume generation so the
        # dashboard can show them for review.
        return _missing_scene_refs(show)
    return True


def reconcile_show(show_id: str, brief: str = "") -> list[str]:
    """Resume the Gate-0 chain for a show, as far as approvals allow.

    Idempotent: re-runs advance() from disk state. Steps that already produced
    artifacts are skipped, interrupted work (e.g. a character whose proposal was
    written but never registered in bootstrap.json) is picked up. Safe to call
    on every dashboard load / poll; the per-show lock serializes against user
    approve/reject actions.
    """
    log_ = run_show_locked(show_id, lambda: BootstrapChain(Show(show_id)).advance(brief=brief))
    return log_


def reconcile_if_stalled(show_id: str, brief: str = "") -> bool:
    """Background self-healing: resume a stuck show, once per show.

    Returns True when a reconcile thread was started. No-op when the show is
    complete, awaiting a human gate, or already being reconciled.
    """
    if show_id in _reconciling:
        return False
    try:
        if not needs_reconcile(show_id):
            return False
    except Exception:
        log.exception("reconcile precheck failed for %s", show_id)
        return False
    _reconciling.add(show_id)

    def _run() -> None:
        try:
            reconcile_show(show_id, brief)
        except Exception:
            log.exception("background reconcile failed for %s", show_id)
        finally:
            ACTIVITY.pop(show_id, None)
            _reconciling.discard(show_id)


def is_bootstrap_reconciling(show_id: str) -> bool:
    """True while a background Gate-0 reconcile thread for this show is alive.

    Lets the dashboard treat an ACTIVITY detail as genuinely in-flight instead
    of a stale string left behind by a finished/crashed job.
    """
    return show_id in _reconciling

    threading.Thread(target=_run, name=f"reconcile-{show_id}", daemon=True).start()
    return True


class BootstrapChain:
    def __init__(self, show: Show, cfg=None, llm: LMStudioClient | None = None,
                 tts: TTSService | None = None, bus=None):
        self.cfg = cfg or get_config()
        self.show = show
        self.llm = llm or LMStudioClient(self.cfg.get("llm", "base_url"))
        self.tts = tts or TTSService(self.cfg)
        self.bus = bus or make_broker(self.cfg["bus"])
        self._auto_override = False

    def _report(self, detail: str) -> None:
        """Expose live progress to the dashboard (thread-safe enough for the
        synchronous approve/reject handlers)."""
        ACTIVITY[self.show.show_id] = {"detail": detail, "ts": time.time()}

    # ---- LLM helpers ----

    def _role_model(self, role: str) -> str:
        roles = self.cfg.get("llm", "roles", {})
        return roles.get(role) or self.cfg.get("llm", "model")

    def _ask(self, role: str, system: str, user: str, temperature: float = 0.7,
             max_tokens: int = 4096) -> dict[str, Any]:
        return self.llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model=self._role_model(role),
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _emit(self, event: Event) -> None:
        log.info("[bootstrap:%s] %s", self.show.show_id, event.type)
        self.bus.publish("bus:approval", event)

    # ---- generation steps ----

    def generate_concept(self, brief: str = "", feedback: str = "") -> dict[str, Any]:
        self._report("Writing concept pitch…")
        profile = self.cfg["show_profile"]
        maturity = profile.get("maturity", "mature")
        current = self.show.read_concept() if feedback.strip() else None
        _tag, user = prompts.concept_prompt(brief, profile, feedback=feedback,
                                            current_draft=current)
        system = prompts.showrunner_system(profile, maturity, profile.get("baseline", "ranma-1-2"))
        concept = self._ask("showrunner", system, user, temperature=0.7)
        concept["maturity"] = maturity  # enforce the approved level exactly
        self.show.write_concept(concept)
        return concept

    def generate_bible(self, feedback: str = "") -> dict[str, Any]:
        self._report("Writing series bible (showrunner LLM)…")
        concept = self.show.read_concept()
        profile = self.cfg["show_profile"]
        maturity = concept.get("maturity", profile.get("maturity", "mature"))
        system = prompts.showrunner_system(profile, maturity, profile.get("baseline", "ranma-1-2"))
        current = self.show.read_bible() if feedback.strip() else None
        bible = self._ask("bible", system, prompts.bible_prompt(concept, profile,
                                                                feedback=feedback,
                                                                current_draft=current),
                          temperature=0.5, max_tokens=8192)
        bible.setdefault("runtime_target_s", profile.get("runtime_target_s", 1320))
        self.show.write_bible(bible)
        return bible

    def generate_character_proposal(self, name: str, feedback: str = "") -> dict[str, Any]:
        self._report(f"Writing character proposal: {name}…")
        bible = self.show.read_bible()
        existing = [self.show.read_character(c) for c in self.show.list_characters()]
        current = next((c for c in existing if c.get("name") == name), None) if feedback.strip() else None
        profile = self.cfg["show_profile"]
        system = prompts.showrunner_system(profile,
                                           bible.get("content_policy", "mature"),
                                           profile.get("baseline", "ranma-1-2"))
        proposal = self._ask("showrunner", system,
                             prompts.character_prompt(bible, existing, name,
                                                      feedback=feedback, current_draft=current),
                             temperature=0.7)
        # force the canon name/id regardless of what the model wrote
        proposal["name"] = name
        proposal["id"] = _slug(name)
        proposal.setdefault("h3_slot", f"@char{len(existing) + 1}")
        self.show.write_character(proposal)
        return proposal

    def generate_scenes(self, feedback: str = "") -> dict[str, Any]:
        self._report("Writing scene registry…")
        import yaml
        bible = self.show.read_bible()
        profile = self.cfg["show_profile"]
        system = prompts.showrunner_system(profile, bible.get("content_policy", "mature"),
                                           profile.get("baseline", "ranma-1-2"))
        current = None
        if feedback.strip():
            locs = []
            for sid in self.show.list_scenes():
                p = self.show.scenes_dir / f"{sid}.yaml"
                try:
                    locs.append(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
                except Exception:
                    continue
            current = {"locations": locs}
        data = self._ask("showrunner", system, prompts.scenes_prompt(bible, feedback=feedback,
                                                                     current_draft=current),
                         temperature=0.6)
        for loc in data.get("locations", []):
            self.show.write_scene(loc)
        return data

    def bootstrap_refs(self, char: dict[str, Any]) -> str:
        """Generate the character reference sheet (Krea 2 on the worker).

        Renders a character portrait from appearance_canonical via the krea2
        ComfyUI instance and drops it in characters/<id>/refs/. Returns 'real'
        when an image landed (opens the CHAR_REFS_PENDING gate), else 'stub'
        so the chain can't block on image infra.
        """
        refs_dir = self.show.character_refs_dir(char.get("id", ""))
        refs_dir.mkdir(parents=True, exist_ok=True)
        refs_json = refs_dir / "refs.json"
        if refs_json.exists():
            try:
                prior = json.loads(refs_json.read_text(encoding="utf-8"))
            except Exception:
                prior = {}
            if prior.get("status") == "real" and any(
                    (refs_dir / f).exists() for f in prior.get("refs", [])):
                return "real"
        krea2 = self.cfg.get("env", "comfyui", {}).get("krea2", {}) or {}
        if not krea2.get("url"):
            # No configured Krea 2 instance (tests/isolated config) -> stay a stub.
            refs_json.write_text(
                '{"status": "stub", "note": "no krea2 url configured (env.yaml comfyui.krea2.url)"}',
                encoding="utf-8")
            return "stub"
        try:
            from .remote import ServiceOps
            name = char.get("name", "") or "character"
            canon = (char.get("appearance_canonical") or "").strip()
            self._report(f"Generating reference image for {name} (Krea 2)…")
            prompt = (f"Anime character reference portrait of {name}. {canon}".rstrip(" .")
                      + ". Full body, front view, neutral standing pose, plain studio "
                        "background, clean lineart, consistent character design, high quality.")
            out = refs_dir / f"{char.get('id', 'char')}_ref_01.png"
            res = ServiceOps(self.cfg).generate_image(
                prompt, seed=0, aspect_ratio="3:4", out_path=str(out))
            if res.get("ok") and out.exists():
                (refs_dir / "refs.json").write_text(json.dumps(
                    {"status": "real", "refs": [out.name], "seed": res.get("seed", 0)},
                    ensure_ascii=False), encoding="utf-8")
                return "real"
        except Exception as exc:
            logging.getLogger(__name__).warning("character ref generation failed: %s", exc)
        (refs_dir / "refs.json").write_text(
            '{"status": "stub", "note": "Krea 2 ref generation unavailable"}',
            encoding="utf-8")
        return "stub"

    def revise_voice_description(self, char: dict[str, Any], feedback: str) -> str:
        """LLM rewrites ONLY the voice_description from rejection feedback."""
        name = char.get("name", "") or "character"
        self._report(f"Revising {name}'s voice description…")
        current = ((char.get("voice", {}) or {}).get("voice_description", "")) or ""
        profile = self.cfg["show_profile"]
        system = prompts.showrunner_system(profile, "mature",
                                           profile.get("baseline", "ranma-1-2"))
        out = self._ask("showrunner", system,
                        prompts.revise_voice_prompt(current, feedback))
        new_vd = ((out or {}).get("voice_description") or "").strip() or current
        char["voice"] = {**(char.get("voice") or {}), "voice_description": new_vd}
        self.show.write_character(char)
        return new_vd

    def revise_character_appearance(self, char: dict[str, Any], feedback: str) -> str:
        """LLM rewrites ONLY appearance_canonical from rejection feedback."""
        name = char.get("name", "") or "character"
        self._report(f"Revising {name}'s appearance for image gen…")
        current = (char.get("appearance_canonical") or "").strip()
        profile = self.cfg["show_profile"]
        system = prompts.showrunner_system(profile, "mature",
                                           profile.get("baseline", "ranma-1-2"))
        out = self._ask("showrunner", system,
                        prompts.revise_appearance_prompt(current, feedback))
        new = ((out or {}).get("appearance_canonical") or "").strip() or current
        char["appearance_canonical"] = new
        self.show.write_character(char)
        return new

    def revise_costume_prompt(self, name: str, current: str, feedback: str) -> str:
        """LLM rewrites ONLY a costume-variant generation prompt from feedback."""
        self._report(f"Revising {name}'s costume from your notes…")
        profile = self.cfg["show_profile"]
        system = prompts.showrunner_system(profile, "mature",
                                           profile.get("baseline", "ranma-1-2"))
        out = self._ask("showrunner", system,
                        prompts.revise_costume_prompt(current, feedback))
        return ((out or {}).get("prompt") or "").strip() or current

    def describe_costume(self, label: str, char_name: str,
                         personality: str = "", situation: str = "",
                         feedback: str = "") -> str:
        """LLM writes a concrete description of ONLY the costume, driven by the
        character's personality + the use case — never their current look.
        """
        self._report(f"Designing costume '{label}' for {char_name}…")
        profile = self.cfg["show_profile"]
        system = prompts.showrunner_system(profile, "mature",
                                           profile.get("baseline", "ranma-1-2"))
        out = self._ask("showrunner", system,
                        prompts.costume_description_prompt(label, char_name,
                                                           personality, situation,
                                                           feedback))
        return ((out or {}).get("costume") or "").strip()

    def revise_scene_setting(self, scene: dict[str, Any], sid: str, feedback: str) -> str:
        """LLM rewrites ONLY a scene's setting_prompt from rejection feedback."""
        import yaml
        name = scene.get("name", "") or sid
        self._report(f"Revising {name}'s setting prompt…")
        current = (scene.get("setting_prompt") or scene.get("description") or "").strip()
        profile = self.cfg["show_profile"]
        system = prompts.showrunner_system(profile, "mature",
                                           profile.get("baseline", "ranma-1-2"))
        out = self._ask("showrunner", system,
                        prompts.revise_scene_setting_prompt(current, feedback))
        new = ((out or {}).get("setting_prompt") or "").strip() or current
        scene["setting_prompt"] = new
        p = self.show.scenes_dir / f"{sid}.yaml"
        if p.exists():
            p.write_text(yaml.safe_dump(scene, sort_keys=False, allow_unicode=True),
                         encoding="utf-8")
        return new

    def bootstrap_voice(self, char: dict[str, Any]) -> None:
        voice = char.get("voice", {})
        name = char.get("name", "") or "character"
        voice_id = f"{char.get('id', 'char')}_voice"
        vd = voice.get("voice_description", "") or ""
        self.show.write_voice({
            "id": voice_id,
            "engine": "qwen3_tts",
            "mode": voice.get("mode", "designed"),
            "speaker": voice.get("speaker"),
            "voice_description": vd,
            "speed": 1.0,
            "pitch": 0.0,
        })
        vc = VoiceConfig(id=voice_id, engine="qwen3_tts",
                         mode=voice.get("mode", "designed"),
                         speaker=voice.get("speaker"),
                         voice_description=vd)
        out = self.show.dir / "assets" / "voice" / f"{voice_id}.wav"
        self._report(f"Synthesizing voice sample for {name} (Qwen3-TTS)…")
        self.tts.synthesize(
            f"Hey, I'm {name}. Let's get to work.",
            vc, out)

    # ---- state machine ----

    def _auto(self) -> bool:
        if self._auto_override:
            return True
        return bool(self.cfg.get("approval", "global", {}).get("auto_approve", False)) \
            or self.cfg.get("approval", "gates", {}).get("show") == "auto"

    def advance(self, brief: str = "", max_steps: int = 200) -> list[str]:
        """Advance the chain as far as approvals allow. Returns a log of actions.

        Step return codes: True = did work, False = blocked on a human gate,
        None = idle (nothing left to do at that step).
        """
        with _show_lock(self.show.show_id):
            return self._advance(brief, max_steps)

    def _advance(self, brief: str = "", max_steps: int = 200) -> list[str]:
        events_log: list[str] = []
        steps = (lambda: self._step_concept(brief, events_log),
                 lambda: self._step_bible(events_log),
                 lambda: self._step_characters(events_log),
                 lambda: self._step_scene_refs(events_log),
                 lambda: self._step_scenes(events_log))
        for _ in range(max_steps):
            progressed = False
            blocked = False
            for step in steps:
                result = step()
                if result is False:
                    blocked = True
                    break
                if result is True:
                    progressed = True
            if blocked or not progressed:
                break
        return events_log

    # Each _step_* returns True (did work), False (blocked on human gate), or None (idle).
    # State is loaded fresh, mutated, and persisted at the end of each step.

    def _step_concept(self, brief: str, log_: list[str]) -> bool | None:
        st = self.show.bootstrap_state()
        c = st.setdefault("concept", {"status": ""})
        if c.get("status") == "approved":
            return None
        if not c.get("status"):
            self.generate_concept(brief)
            c["status"] = "pending"
            self._emit(new_event(CONCEPT_PENDING, show_id=self.show.show_id))
            log_.append("concept: generated -> pending")
        if not self._auto():
            self.show.set_bootstrap_state(st)
            return False
        c["status"] = "approved"
        self._emit(new_event(CONCEPT_APPROVED, show_id=self.show.show_id,
                             payload={"auto_approved": True}))
        self.show.set_bootstrap_state(st)
        log_.append("concept: approved (auto)")
        return True

    def _step_bible(self, log_: list[str]) -> bool | None:
        st = self.show.bootstrap_state()
        b = st.setdefault("bible", {"status": ""})
        if b.get("status") == "approved":
            return None
        if not b.get("status"):
            self.generate_bible()
            b["status"] = "pending"
            self._emit(new_event(BIBLE_PENDING, show_id=self.show.show_id))
            self.show.set_bootstrap_state(st)
            log_.append("bible: generated -> pending")
        if not self._auto():
            return False
        b["status"] = "approved"
        self._emit(new_event(BIBLE_APPROVED, show_id=self.show.show_id,
                             payload={"auto_approved": True}))
        self.show.set_bootstrap_state(st)
        log_.append("bible: approved (auto)")
        return True

    def _step_characters(self, log_: list[str]) -> bool | None:
        st = self.show.bootstrap_state()
        chars = st.setdefault("characters", [])
        roster = [c.get("name") for c in self.show.read_bible().get("cast", [])]
        roster = [n for n in roster if n]
        if not roster:
            return None
        did = False

        for name in roster:
            char_state = next((x for x in chars if x.get("name") == name), None)
            if char_state is None:
                char_state = {"name": name, "proposal": "", "refs": "", "voice": ""}
                chars.append(char_state)
                # Write-through: persist the new roster entry BEFORE any
                # long-running asset work so an interrupted advance() (crash /
                # restart) never loses it. The chain resumes from disk state.
                self.show.set_bootstrap_state(st)

            def _char():
                return next((self.show.read_character(c) for c in self.show.list_characters()
                             if self.show.read_character(c).get("name") == name), None)

            proposed_names = {c.get("name") for c in
                              (self.show.read_character(cid) for cid in self.show.list_characters())}
            char = _char()
            if name not in proposed_names:
                proposed = self.generate_character_proposal(name)
                self._emit(new_event(CHAR_PROPOSAL_PENDING, show_id=self.show.show_id,
                                     payload={"char": proposed.get("id")}))
                log_.append(f"character {name}: proposal generated -> pending")
                did = True
                char = _char()
            if not char:
                continue

            # Build the full review package (text + ref image + voice sample)
            # BEFORE the single human gate, so the approval is on the whole
            # character — not just the text.
            if char_state.get("refs") == "":
                kind = self.bootstrap_refs(char)
                char_state["refs"] = kind
                self.show.set_bootstrap_state(st)   # write-through: refs result survives a crash
                log_.append(f"character {name}: refs ({kind})")
                did = True
                if kind == "real":
                    self._emit(new_event(CHAR_REFS_PENDING, show_id=self.show.show_id,
                                         payload={"char": char.get("id")}))
            if char_state.get("voice") == "":
                self.bootstrap_voice(char)
                char_state["voice"] = "pending"
                self.show.set_bootstrap_state(st)   # write-through: voice state survives a crash
                self._emit(new_event(VOICE_SAMPLE_PENDING, show_id=self.show.show_id,
                                     payload={"char": char.get("id")}))
                log_.append(f"character {name}: voice sample -> pending")
                did = True

            # Refs gate independently: the generated reference image needs human
            # approval even if the proposal text was auto-approved. Only auto
            # when the master auto_approve switch is on.
            if char_state.get("refs") not in ("approved", "", None):
                if not self._auto():
                    self.show.set_bootstrap_state(st)
                    return False
                char_state["refs"] = "approved"
                self._emit(new_event(CHAR_REFS_APPROVED, show_id=self.show.show_id,
                                     payload={"char": char.get("id"), "auto_approved": True}))
                log_.append(f"character {name}: refs approved (auto)")
                did = True

            if char_state.get("proposal") != "approved" or char_state.get("voice") != "approved":
                if not self._auto():
                    self.show.set_bootstrap_state(st)
                    return False
                char_state["proposal"] = "approved"
                char_state["voice"] = "approved"
                self._emit(new_event(CHAR_PROPOSAL_APPROVED, show_id=self.show.show_id,
                                     payload={"char": char.get("id"), "auto_approved": True}))
                self._emit(new_event(VOICE_APPROVED, show_id=self.show.show_id,
                                     payload={"char": char.get("id"), "auto_approved": True}))
                log_.append(f"character {name}: approved (auto)")
                did = True

        if did:
            self.show.set_bootstrap_state(st)
        return True if did else None

    def _step_scenes(self, log_: list[str]) -> bool | None:
        st = self.show.bootstrap_state()
        s = st.setdefault("scenes", {"status": ""})
        if s.get("status") == "approved":
            st["complete"] = True
            self.show.set_bootstrap_state(st)
            return None
        if not s.get("status"):
            self.generate_scenes()
            s["status"] = "pending"
            self._emit(new_event(SCENE_REGISTRY_PENDING, show_id=self.show.show_id))
            self.show.set_bootstrap_state(st)
            log_.append("scenes: generated -> pending")
        if not self._auto():
            return False
        s["status"] = "approved"
        self._emit(new_event(SCENE_REGISTRY_APPROVED, show_id=self.show.show_id,
                             payload={"auto_approved": True}))
        st["complete"] = True
        self.show.set_bootstrap_state(st)
        self._emit(new_event(BOOTSTRAP_COMPLETE, show_id=self.show.show_id))
        log_.append("scenes: approved -> BOOTSTRAP COMPLETE")
        return True

    def _step_scene_refs(self, log_: list[str]) -> bool | None:
        """Generate a reference image for every scene once the registry exists.

        Runs while the registry is still *pending* (so the human can review the
        locations on the dashboard) and again after approval. Non-gating: images
        drop in as they finish; the chain never blocks on them.
        """
        import yaml
        st = self.show.bootstrap_state()
        if not (st.get("scenes", {}) or {}).get("status"):
            return None
        did = False
        for sid in self.show.list_scenes():
            rd = self.show.scenes_dir / sid / "refs"
            if (rd / f"{sid}_ref_01.png").exists():
                continue
            try:
                scene = yaml.safe_load((self.show.scenes_dir / f"{sid}.yaml")
                                       .read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            ok = self.bootstrap_scene_ref(scene, sid)
            if ok:
                log_.append(f"scene {sid}: ref image")
                did = True
        return True if did else None

    def bootstrap_scene_ref(self, scene: dict[str, Any], sid: str) -> bool:
        """Generate a location reference image (krea2) for one scene.

        Returns True when an image landed (or a stub was recorded so reconcile
        stops retrying). A stub refs.json prevents an infinite reconcile loop
        when no krea2 instance is configured or generation is unavailable.
        """
        rd = self.show.scenes_dir / sid / "refs"
        rd.mkdir(parents=True, exist_ok=True)
        refs_json = rd / "refs.json"
        if (rd / f"{sid}_ref_01.png").exists():
            return True
        try:
            from .remote import ServiceOps
            krea2 = self.cfg.get("env", "comfyui", {}).get("krea2", {}) or {}
            if not krea2.get("url"):
                refs_json.write_text(
                    '{"status": "stub", "note": "no krea2 url configured (env.yaml comfyui.krea2.url)"}',
                    encoding="utf-8")
                return False
            name = scene.get("name", "") or sid
            setting = (scene.get("setting_prompt") or scene.get("description") or "").strip()
            self._report(f"Generating scene image: {name} (Krea 2)…")
            prompt = (f"Anime location concept art. {setting}".rstrip(" .")
                      + ". Establishing shot, cinematic composition, detailed background "
                        "art, consistent series art style, high quality, no people.")
            out = rd / f"{sid}_ref_01.png"
            res = ServiceOps(self.cfg).generate_image(prompt, seed=0, aspect_ratio="16:9",
                                                      out_path=str(out))
            if res.get("ok") and out.exists():
                refs_json.write_text(json.dumps(
                    {"status": "real", "refs": [out.name], "seed": res.get("seed", 0)},
                    ensure_ascii=False), encoding="utf-8")
                return True
            refs_json.write_text(
                '{"status": "stub", "note": "krea2 scene ref generation unavailable"}',
                encoding="utf-8")
        except Exception as exc:
            log.warning("scene ref generation failed for %s: %s", sid, exc)
            try:
                refs_json.write_text(
                    f'{{"status": "stub", "note": "scene ref failed: {exc}"}}',
                    encoding="utf-8")
            except Exception:
                pass
        return False


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").strip().lower()).strip("-")