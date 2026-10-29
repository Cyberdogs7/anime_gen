"""Bootstrap chain (Gate 0) - Concept -> Bible -> Characters (proposal/refs/voice) -> Scenes.

Drives the iterative bootstrap (DESIGN.md §9.5a) with the Showrunner LLM.
State lives in shows/<id>/bootstrap.json; approval events are appended to a
local audit log. Reject-with-notes regenerates only the current step (approved
steps stay locked).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import prompts
from .events import (
    BIBLE_APPROVED, BIBLE_PENDING, BOOTSTRAP_COMPLETE, CHAR_PROPOSAL_APPROVED,
    CHAR_PROPOSAL_PENDING, CHAR_REFS_APPROVED, CHAR_REFS_PENDING, CONCEPT_APPROVED,
    CONCEPT_PENDING, SCENE_REGISTRY_APPROVED, SCENE_REGISTRY_PENDING, VOICE_APPROVED,
    VOICE_SAMPLE_PENDING, Event, new_event, record,
)
from .clients import LMStudioClient, TTSService
from .clients.tts import VoiceConfig
from .config import DEFAULT_RUNTIME_TARGET_S, get_config
from .names import looks_like_character_name
from .show import Show

log = logging.getLogger(__name__)

STEPS = ("concept", "bible", "characters", "scenes")

# Live progress for the CLI: show_id -> {"detail": str, "ts": float}.
# Written by BootstrapChain._report while a synchronous approve/reject request
# is running; cleared by approval.py once the chain finishes.
ACTIVITY: dict[str, dict[str, Any]] = {}

# Per-show locks: approve/reject clicks and agent cards must never interleave
# for the same show. RLock because advance() may be called from a caller that
# already holds it.
_show_locks: dict[str, threading.RLock] = {}
_show_locks_guard = threading.Lock()


def _show_lock(show_id: str) -> threading.RLock:
    with _show_locks_guard:
        lock = _show_locks.get(show_id)
        if lock is None:
            lock = threading.RLock()
            _show_locks[show_id] = lock
        return lock


def _voice_sample_fallback(char: dict[str, Any], name: str) -> str:
    role = str(char.get("role") or "").strip()
    notes = str(char.get("appearance_notes") or "").strip()
    traits = str(char.get("traits_for_llm") or "").strip()
    personality = ", ".join(str(x) for x in (char.get("personality") or []))
    lines = [f"This is {name}."]
    if role:
        lines.append(role)
    if personality:
        lines.append(f"People who know me would say I am {personality.lower()}.")
    if notes:
        lines.append(notes)
    if traits:
        lines.append(traits)
    lines.append("That is who I am. Now tell me what you want.")
    return " ".join(x for x in lines if x)


class BootstrapChain:
    def __init__(self, show: Show, cfg=None, llm: LMStudioClient | None = None,
                 tts: TTSService | None = None):
        self.cfg = cfg or get_config()
        self.show = show
        self.llm = llm or LMStudioClient(self.cfg.get("llm", "base_url"))
        self.tts = tts or TTSService(self.cfg)
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
            role=role,
        )

    def _emit(self, event: Event) -> None:
        log.info("[bootstrap:%s] %s", self.show.show_id, event.type)
        record(event, self.show.dir)

    # ---- generation steps ----

    def generate_concept(self, brief: str = "", feedback: str = "") -> dict[str, Any]:
        self._report("Writing concept pitch…")
        profile = self.show.style_profile(self.cfg)
        maturity = profile.get("maturity") or ""
        current = self.show.read_concept() if feedback.strip() else None
        _tag, user = prompts.concept_prompt(brief, profile, feedback=feedback,
                                            current_draft=current)
        system = prompts.showrunner_system(profile, maturity, profile.get("baseline", ""))
        temp = 0.5 if feedback.strip() else 1.05
        concept = self._ask("showrunner", system, user, temperature=temp)
        if maturity.strip():
            concept["maturity"] = maturity  # enforce the approved level exactly
        self.show.write_concept(concept)
        return concept

    def generate_bible(self, feedback: str = "") -> dict[str, Any]:
        self._report("Writing series bible (showrunner LLM)…")
        concept = self.show.read_concept()
        profile = self.show.style_profile(self.cfg)
        maturity = str(concept.get("maturity") or profile.get("maturity") or "").strip()
        system = prompts.showrunner_system(profile, maturity, profile.get("baseline", ""))
        current = self.show.read_bible() if feedback.strip() else None
        # Revisions run cool: heat is for fresh invention, discipline is for
        # following feedback. Hot revision passes drift instead of complying.
        temp = 0.5 if feedback.strip() else 1.0
        bible = self._ask("bible", system, prompts.bible_prompt(concept, profile,
                                                                feedback=feedback,
                                                                current_draft=current),
                           temperature=temp, max_tokens=8192)
        # The approved concept's maturity IS the show's content policy; persisting
        # it here is what lets every later stage read the show's level instead of
        # falling back to a code default.
        if maturity:
            bible["content_policy"] = maturity
        # Keep the generation style field and its bible-only reference split even
        # if the model omitted the empty reference block.
        bible.setdefault("art_direction", {})
        bible.setdefault("runtime_target_s", profile.get("runtime_target_s", DEFAULT_RUNTIME_TARGET_S))
        self.show.write_bible(bible)
        return bible

    def generate_character_proposal(self, name: str, feedback: str = "") -> dict[str, Any]:
        self._report(f"Writing character proposal: {name}…")
        bible = self.show.read_bible()
        existing = [self.show.read_character(c) for c in self.show.list_characters()]
        current = next((c for c in existing if c.get("name") == name), None) if feedback.strip() else None
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile,
                                           bible.get("content_policy") or "",
                                           profile.get("baseline", ""))
        temp = 0.5 if feedback.strip() else 1.0
        proposal = self._ask("showrunner", system,
                             prompts.character_prompt(bible, existing, name,
                                                      feedback=feedback, current_draft=current),
                              temperature=temp)
        # force the canon name/id regardless of what the model wrote
        proposal["name"] = name
        proposal["id"] = _slug(name)
        proposal.setdefault("h3_slot", f"@char{len(existing) + 1}")
        if len(str(proposal.get("voice_sample_text") or "").split()) < 50:
            extra = self._ask("showrunner", system,
                              prompts.voice_sample_prompt(proposal, bible),
                               temperature=1.0)
            text = str(extra.get("voice_sample_text") or "").strip()
            if text:
                proposal["voice_sample_text"] = text
        self.show.write_character(proposal)
        return proposal

    def generate_scenes(self, feedback: str = "") -> dict[str, Any]:
        self._report("Writing scene registry…")
        import yaml
        bible = self.show.read_bible()
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, bible.get("content_policy") or "",
                                           profile.get("baseline", ""))
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
        temp = 0.5 if feedback.strip() else 1.0
        data = self._ask("showrunner", system, prompts.scenes_prompt(bible, feedback=feedback,
                                                                     current_draft=current),
                          temperature=temp)
        with _show_lock(self.show.show_id):
            st = self.show.bootstrap_state()
            sts = st.setdefault("scenes", {"status": ""})
            locs = sts.setdefault("locations", {})
            written = []
            for loc in data.get("locations", []):
                self.show.write_scene(loc)
                sid = str(loc.get("id") or "")
                if sid:
                    written.append(sid)
                    locs.setdefault(sid, "pending")
            # Drop registry entries for locations the model removed (unless
            # already approved — approvals are never silently discarded).
            for sid in [k for k in list(locs)
                        if k not in written and locs.get(k) != "approved"]:
                del locs[sid]
            self.show.set_bootstrap_state(st)
        return data

    def revise_voice_description(self, char: dict[str, Any], feedback: str) -> str:
        """LLM rewrites ONLY the voice_description from rejection feedback."""
        name = char.get("name", "") or "character"
        self._report(f"Revising {name}'s voice description…")
        current = ((char.get("voice", {}) or {}).get("voice_description", "")) or ""
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, (self.show.read_bible() or {}).get("content_policy") or "",
                                           profile.get("baseline", ""))
        out = self._ask("showrunner", system,
                        prompts.revise_voice_prompt(current, feedback))
        new_vd = ((out or {}).get("voice_description") or "").strip() or current
        char["voice"] = {**(char.get("voice") or {}), "voice_description": new_vd}
        self.show.write_character(char)
        return new_vd

    def refresh_voice(self, name: str, notes: str = "",
                      model: str = "") -> str:
        """Full voice redo for one character: identity + sample text.

        Rewrites the voice description from director notes (if any), then the
        sample monologue, and marks the voice pending. TTS re-recording stays
        with the caller (audio lane). Used by every voice reject path so a
        reject anywhere rebuilds everything instead of one slice.
        """
        char = None
        for cid in self.show.list_characters():
            c = self.show.read_character(cid) or {}
            if str(c.get("name") or "").lower() == str(name or "").strip().lower():
                char = c
                break
        if not char:
            raise ValueError(f"no character named {name!r}")
        notes = (notes or "").strip()
        if notes:
            self.revise_voice_description(char, notes)
            char = self.show.read_character(char.get("id") or "") or char
        bible = self.show.read_bible() or {}
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, bible.get("content_policy") or "",
                                           profile.get("baseline", ""))
        prompt = prompts.voice_sample_prompt(char, bible)
        if notes:
            prompt = prompt.rstrip() + f"\n\nDirector's notes for this rewrite:\n{notes}"
        model = model or (self.cfg.get("llm", "roles", {}).get("showrunner")
                          or self.cfg.get("llm", "model") or "")
        data = self.llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}],
            model=model, temperature=0.8)
        text = str((data or {}).get("voice_sample_text") or "").strip()
        if len(text.split()) < 50:
            raise ValueError("model returned a voice sample that is too short")
        char["voice_sample_text"] = text
        self.show.write_character(char)
        st = self.show.bootstrap_state()
        for entry in st.get("characters", []):
            if str(entry.get("name") or "").lower() == str(name or "").strip().lower():
                entry["voice"] = ""
        self.show.set_bootstrap_state(st)
        return text

    def revise_character_appearance(self, char: dict[str, Any], feedback: str) -> str:
        """LLM rewrites ONLY appearance_canonical from rejection feedback."""
        name = char.get("name", "") or "character"
        self._report(f"Revising {name}'s appearance for image gen…")
        current = (char.get("appearance_canonical") or "").strip()
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, (self.show.read_bible() or {}).get("content_policy") or "",
                                           profile.get("baseline", ""))
        out = self._ask("showrunner", system,
                        prompts.revise_appearance_prompt(current, feedback))
        new = ((out or {}).get("appearance_canonical") or "").strip() or current
        char["appearance_canonical"] = new
        self.show.write_character(char)
        return new

    def revise_costume_prompt(self, name: str, current: str, feedback: str) -> str:
        """LLM rewrites ONLY a costume-variant generation prompt from feedback."""
        self._report(f"Revising {name}'s costume from your notes…")
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, (self.show.read_bible() or {}).get("content_policy") or "",
                                           profile.get("baseline", ""))
        out = self._ask("showrunner", system,
                        prompts.revise_costume_prompt(current, feedback))
        return ((out or {}).get("prompt") or "").strip() or current

    def describe_costume(self, label: str, char_name: str,
                         personality: str = "", situation: str = "",
                         feedback: str = "") -> str:
        """LLM writes a concrete description of ONLY the costume, driven by the
        label (and the character's personality) — never their current look, and
        never embellished beyond what the label implies.
        """
        self._report(f"Designing costume '{label}' for {char_name}…")
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, (self.show.read_bible() or {}).get("content_policy") or "",
                                           profile.get("baseline", ""))
        out = self._ask("showrunner", system,
                        prompts.costume_description_prompt(label, char_name,
                                                           personality, situation,
                                                           feedback))
        return ((out or {}).get("costume") or "").strip()

    def describe_object(self, name: str, context: str = "") -> str:
        """LLM writes a concrete visual description of ONE recurring prop.

        A bare object name makes the image model invent a different look every
        time; this turns it into positive, renderable prose (no negations).
        """
        self._report(f"Designing prop '{name}'.")
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, (self.show.read_bible() or {}).get("content_policy") or "",
                                           profile.get("baseline", ""))
        out = self._ask("showrunner", system,
                        prompts.object_description_prompt(name, context))
        return ((out or {}).get("object") or "").strip()

    def revise_scene_setting(self, scene: dict[str, Any], sid: str, feedback: str) -> str:
        """LLM rewrites ONLY a scene's setting_prompt from rejection feedback."""
        import yaml
        name = scene.get("name", "") or sid
        self._report(f"Revising {name}'s setting prompt…")
        current = (scene.get("setting_prompt") or scene.get("description") or "").strip()
        profile = self.show.style_profile(self.cfg)
        system = prompts.showrunner_system(profile, (self.show.read_bible() or {}).get("content_policy") or "",
                                           profile.get("baseline", ""))
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
        # New characters are voice-DESIGNED from their sheet; 'preset' is kept only
        # for legacy sheets that already chose a named speaker.
        mode = str(voice.get("mode") or "designed").strip().lower()
        if mode not in ("preset", "designed"):
            mode = "designed"
        vd = voice.get("voice_description", "") or ""
        if mode == "designed" and not vd.strip():
            traits = ", ".join(char.get("personality") or []) or \
                str(char.get("traits_for_llm") or "")
            vd = (f"adult voice matching {name}'s character"
                  + (f": {traits}" if traits else "") + ".")
        self.show.write_voice({
            "id": voice_id,
            "engine": "qwen3_tts",
            "mode": mode,
            "speaker": voice.get("speaker") if mode == "preset" else None,
            "voice_description": vd,
            "speed": 1.0,
            "pitch": 0.0,
        })
        vc = VoiceConfig(id=voice_id, engine="qwen3_tts",
                         mode=mode,
                         speaker=voice.get("speaker") if mode == "preset" else None,
                         voice_description=vd)
        out = self.show.dir / "assets" / "voice" / f"{voice_id}.wav"
        sample_text = str(char.get("voice_sample_text") or "").strip()
        if len(sample_text.split()) < 50:
            sample_text = _voice_sample_fallback(char, name)
        self._report(f"Synthesizing voice sample for {name} (Qwen3-TTS)…")
        self.tts.synthesize(sample_text, vc, out)
        # The designed sample becomes the character's reusable voice identity: from
        # here every line CLONES this file (fixed timbre), never re-designs it.
        if mode == "designed" and self.tts.health():
            self.show.write_voice({
                "id": voice_id,
                "engine": "qwen3_tts",
                "mode": "clone",
                "speaker": None,
                "voice_description": vd,
                "clone_ref": f"assets/voice/{voice_id}.wav",
                "clone_text": sample_text,
                "clone_cache": f"voices/{voice_id}.pt",
                "speed": 1.0,
                "pitch": 0.0,
            })

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

    # ---- split Gate-0 passes (one GPU service per board card) ----

    def advance_plan(self, brief: str = "") -> list[str]:
        """Generate only the text artifacts: concept, bible, proposals, scenes."""
        with _show_lock(self.show.show_id):
            return self._advance_plan(brief)

    def _advance_plan(self, brief: str) -> list[str]:
        st = self.show.bootstrap_state()
        log_: list[str] = []
        concept = st.setdefault("concept", {"status": ""})
        if concept.get("status") != "approved" and not concept.get("status"):
            self.generate_concept(brief)
            concept["status"] = "pending"
            self._emit(new_event(CONCEPT_PENDING, show_id=self.show.show_id))
            log_.append("concept: generated -> pending")
            self.show.set_bootstrap_state(st)
        bible = st.setdefault("bible", {"status": ""})
        if bible.get("status") != "approved" and not bible.get("status"):
            self.generate_bible()
            bible["status"] = "pending"
            self._emit(new_event(BIBLE_PENDING, show_id=self.show.show_id))
            log_.append("bible: generated -> pending")
            self.show.set_bootstrap_state(st)
        roster = [c.get("name") for c in self.show.read_bible().get("cast", [])]
        roster = [n for n in roster if n and looks_like_character_name(n)]
        chars = st.setdefault("characters", [])
        for name in roster:
            entry = next((c for c in chars if c.get("name") == name), None)
            if entry is None:
                entry = {"name": name, "proposal": "", "refs": "", "voice": ""}
                chars.append(entry)
            if entry.get("proposal") != "approved" and not entry.get("proposal"):
                existing = next((cid for cid in self.show.list_characters()
                                 if self.show.read_character(cid).get("name") == name),
                                None)
                if not existing:
                    self.generate_character_proposal(name)
                entry["proposal"] = "pending"
                self._emit(new_event(CHAR_PROPOSAL_PENDING, show_id=self.show.show_id,
                                     payload={"char": _slug(name)}))
                log_.append(f"character {name}: proposal -> pending")
                self.show.set_bootstrap_state(st)
        scenes = st.setdefault("scenes", {"status": ""})
        if scenes.get("status") != "approved" and not scenes.get("status"):
            self.generate_scenes()
            scenes["status"] = "pending"
            self._emit(new_event(SCENE_REGISTRY_PENDING, show_id=self.show.show_id))
            log_.append("scenes: generated -> pending")
            self.show.set_bootstrap_state(st)
        self.show.set_bootstrap_state(st)
        return log_

    # ---- single-stage Gate-0 passes (one board card each) ----

    def advance_ideas(self, brief: str = "", n: int = 5,
                      notes: str = "") -> list[str]:
        """Pitch N distinct executions of the brief and archive them all.

        Writes ``ideas.json`` (every pitch kept with ``status`` ``proposed``).
        Picking pitches happens via the board (approval = concept). ``notes``
        are owner guidance for this round (e.g. from a re-pitch) and are
        appended to the brief the model sees.
        """
        from .proposals import propose_concepts
        with _show_lock(self.show.show_id):
            log_: list[str] = []
            guided = (brief + "\n\nOwner notes for this round: " + notes.strip()
                      if notes.strip() else brief)
            pitches = propose_concepts(guided, n=n, cfg=self.cfg, llm=self.llm)
            import time as _time
            # Append-only: re-pitches add to the archive, never erase picks.
            archive = self.show.read_ideas() or {}
            existing = archive.get("pitches") or []
            taken = {str(p.get("show_id") or "") for p in existing if p.get("show_id")}
            base = len(existing)
            new_pitches = []
            for i, p in enumerate(pitches or []):
                # The pitch carries its own show id: prefer a usable
                # model-provided slug, else derive one from its title, deduped.
                slug = _pitch_slug(str((p or {}).get("show_id") or ""))
                if not slug:
                    slug = _pitch_slug(str((p or {}).get("title") or "")) or "show"
                root = slug[:40].strip("-") or "show"
                slug, k = root, 2
                while slug in taken:
                    slug = f"{root}-{k}"
                    k += 1
                taken.add(slug)
                entry = {"id": f"p{base + i + 1}", **(p or {}),
                         "show_id": slug, "status": "proposed",
                         "selected_at": None}
                new_pitches.append(entry)
            existing.extend(new_pitches)
            archive.update({
                "brief": brief or archive.get("brief", ""),
                "notes": notes.strip(),
                "n": len(existing),
                "created": archive.get("created") or int(_time.time()),
                "updated": int(_time.time()),
                "pitches": existing,
            })
            self.show.write_ideas(archive)
            log_.append(f"ideas: {len(pitches or [])} new pitches "
                        f"({len(existing)} archived) -> pending pick")
            self.show.set_bootstrap_state(self.show.bootstrap_state())
            return log_

    def advance_bible(self, notes: str = "", force: bool = False) -> list[str]:
        """Generate only the bible from the approved concept (lands pending).

        ``notes`` is reviewer feedback from a rejected round: it is passed
        through so the rewrite addresses it instead of re-rolling the same
        content. ``force`` re-arms a ``pending`` stage (a reject leaves the
        old status in place; without re-arming the guard below would skip
        generation and the revision would no-op).
        """
        with _show_lock(self.show.show_id):
            log_: list[str] = []
            st = self.show.bootstrap_state()
            if force:
                st["bible"] = {"status": ""}
                self.show.set_bootstrap_state(st)
            bible = st.setdefault("bible", {"status": ""})
            if bible.get("status") != "approved" and not bible.get("status"):
                self.generate_bible(feedback=notes)
                bible["status"] = "pending"
                self._emit(new_event(BIBLE_PENDING, show_id=self.show.show_id))
                log_.append("bible: generated -> pending")
                self.show.set_bootstrap_state(st)
            return log_

    def advance_characters(self, notes: str = "", force: bool = False) -> list[str]:
        """Generate only the character proposals from the approved bible.

        ``notes`` is reviewer feedback: only pending (unapproved) proposals
        are regenerated, so the feedback lands exactly on the rejected ones.
        ``force`` re-arms ``pending`` proposals (a reject leaves them in
        place), otherwise the revision would no-op.
        """
        with _show_lock(self.show.show_id):
            log_: list[str] = []
            st = self.show.bootstrap_state()
            if force:
                for entry in st.get("characters", []):
                    if entry.get("proposal") == "pending":
                        entry["proposal"] = ""
                self.show.set_bootstrap_state(st)
            roster = [c.get("name") for c in self.show.read_bible().get("cast", [])]
            roster = [n for n in roster if n and looks_like_character_name(n)]
            chars = st.setdefault("characters", [])
            for name in roster:
                entry = next((c for c in chars if c.get("name") == name), None)
                if entry is None:
                    entry = {"name": name, "proposal": "", "refs": "", "voice": ""}
                    chars.append(entry)
                if entry.get("proposal") != "approved" and not entry.get("proposal"):
                    existing = next((cid for cid in self.show.list_characters()
                                     if self.show.read_character(cid).get("name") == name),
                                    None)
                    # A forced rerun (rejected with notes) must regenerate
                    # even when files exist, or the worker runs 3 minutes
                    # and changes nothing.
                    if not existing or force:
                        self.generate_character_proposal(name, feedback=notes)
                    entry["proposal"] = "pending"
                    self._emit(new_event(CHAR_PROPOSAL_PENDING, show_id=self.show.show_id,
                                         payload={"char": _slug(name)}))
                    log_.append(f"character {name}: proposal -> pending")
                    self.show.set_bootstrap_state(st)
            return log_

    def advance_scene_registry(self, notes: str = "", force: bool = False
                             ) -> list[str]:
        """Generate only the scene registry (lands pending).

        ``notes`` is reviewer feedback from a rejected round. ``force``
        re-arms a ``pending`` stage so the revision regenerates instead of
        no-op'ing.
        """
        with _show_lock(self.show.show_id):
            log_: list[str] = []
            st = self.show.bootstrap_state()
            scenes = st.setdefault("scenes", {"status": ""})
            if force:
                # Re-arm without discarding approvals: approved locations
                # stay approved, everything else regenerates.
                scenes["status"] = ""
                self.show.set_bootstrap_state(st)
            if scenes.get("status") != "approved":
                self.generate_scenes(feedback=notes)
                scenes["status"] = "pending"
                self._emit(new_event(SCENE_REGISTRY_PENDING, show_id=self.show.show_id))
                log_.append("scenes: generated -> pending")
                self.show.set_bootstrap_state(st)
            return log_

    def advance_images(self, notes: str = "") -> list[str]:
        """Generate character and scene reference images only (ComfyUI).

        A retake (notes present) also regenerates entries marked rejected,
        not just missing ones; per-asset director notes ride along.
        """
        with _show_lock(self.show.show_id):
            return self._advance_images(notes=notes)

    def _advance_images(self, notes: str = "") -> list[str]:
        from . import refs as _refs
        show = self.show
        st = show.bootstrap_state()
        log_: list[str] = []
        do_force = bool((notes or "").strip())
        for entry in st.get("characters", []):
            if entry.get("refs") not in ("", None, "stub", "rejected"):
                continue
            name = entry.get("name") or ""
            char = next((show.read_character(cid)
                         for cid in show.list_characters()
                         if show.read_character(cid).get("name") == name), None)
            if not char:
                continue
            entry_notes = str(entry.get("rejected_notes") or notes or "").strip()
            kind = _refs.ensure_ref(show, char, cfg=self.cfg, llm=None,
                                    notes=entry_notes, force=do_force,
                                    report=self._report)
            show.set_bootstrap_state(st)
            log_.append(f"character {name}: refs ({kind})")
            if kind == "real":
                self._emit(new_event(CHAR_REFS_PENDING, show_id=show.show_id,
                                     payload={"char": char.get("id")}))
        self._step_scene_refs(log_, notes=notes)
        show.set_bootstrap_state(st)
        return log_

    def advance_voices(self, char: str = "") -> list[str]:
        """Generate voice samples only (TTS). Optionally one character."""
        if isinstance(char, dict):
            char = char.get("name") or char.get("id") or ""
        with _show_lock(self.show.show_id):
            return self._advance_voices(str(char or ""))

    def _advance_voices(self, char: str = "") -> list[str]:
        st = self.show.bootstrap_state()
        if isinstance(char, dict):
            char = char.get("name") or char.get("id") or ""
        char_filter = str(char or "").strip().lower()
        log_: list[str] = []
        for entry in st.get("characters", []):
            name = str(entry.get("name") or "")
            if char_filter and name.lower() != char_filter:
                continue
            # Rejected voices regenerate like rejected image refs do: without
            # this a main-card rerun silently skips the rejected voice and the
            # retake never happens.
            if entry.get("voice") not in ("", None, "rejected"):
                continue
            char_data = next((self.show.read_character(cid)
                              for cid in self.show.list_characters()
                              if self.show.read_character(cid).get("name") == name), None)
            if not char_data:
                continue
            self.bootstrap_voice(char_data)
            entry["voice"] = "pending"
            self.show.set_bootstrap_state(st)
            log_.append(f"character {name}: voice sample -> pending")
            self._emit(new_event(VOICE_SAMPLE_PENDING, show_id=self.show.show_id,
                                 payload={"char": char_data.get("id")}))
        return log_

    def check_complete(self) -> bool:
        st = self.show.bootstrap_state()
        ok = ((st.get("concept") or {}).get("status") == "approved"
              and (st.get("bible") or {}).get("status") == "approved"
              and (st.get("scenes") or {}).get("status") == "approved"
              and all(c.get("proposal") == "approved"
                      and c.get("refs") == "approved"
                      and c.get("voice") == "approved"
                      for c in st.get("characters", [])))
        if ok and not st.get("complete"):
            st["complete"] = True
            self.show.set_bootstrap_state(st)
            self._emit(new_event(BOOTSTRAP_COMPLETE, show_id=self.show.show_id))
        return ok

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
        roster = [n for n in roster if n and looks_like_character_name(n)]
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
                from . import refs as _refs
                kind = _refs.ensure_ref(self.show, char, cfg=self.cfg, llm=self.llm,
                                        report=self._report)
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

    def _step_scene_refs(self, log_: list[str], notes: str = "") -> bool | None:
        """Generate a reference image for every scene once the registry exists.

        Runs while the registry is still *pending* (so the human can review the
        locations on the dashboard) and again after approval. Non-gating: images
        drop in as they finish; the chain never blocks on them. Existing files
        are left alone unless their refs status is rejected (rework requested).
        """
        import yaml
        st = self.show.bootstrap_state()
        if not (st.get("scenes") or {}).get("status"):
            return None
        did = False
        from . import scenes as _scenes
        for sid in self.show.list_scenes():
            rd = self.show.scenes_dir / sid / "refs"
            out = rd / f"{sid}_ref_01.png"
            try:
                status = str((_scenes.load_refs(self.show, sid) or {}).get("status") or "")
            except Exception:
                status = ""
            if out.exists() and status not in ("", None, "rejected"):
                continue
            try:
                scene = yaml.safe_load((self.show.scenes_dir / f"{sid}.yaml")
                                       .read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            snap = ""
            try:
                snap = str(((_scenes.load_refs(self.show, sid) or {}).get("rejected_notes")) or "")
            except Exception:
                pass
            ok = self.bootstrap_scene_ref(scene, sid,
                                          force=status == "rejected",
                                          notes=snap or notes)
            if ok:
                log_.append(f"scene {sid}: ref image")
                did = True
        return True if did else None

    def refresh_scene(self, sid: str, notes: str, model: str = "") -> dict:
        """Rewrite one location's description/setting from director notes.

        Used when a location reject asks for a different *place* (not just a
        different rendering): appended prompt notes lose to a dominant base
        description, so the yaml itself has to change first. Returns the
        updated scene dict.
        """
        import yaml as _yaml
        p = self.show.scenes_dir / f"{sid}.yaml"
        scene: dict[str, Any] = {}
        if p.exists():
            try:
                scene = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                scene = {}
        model = model or (self.cfg.get("llm", "roles", {}).get("showrunner")
                          or self.cfg.get("llm", "model") or "")
        system = ("You are rewriting one location brief for an anime production "
                  "bible. Reply with exactly one JSON object, no other text.")
        user = ("Current location brief (JSON):\n"
                + json.dumps(scene, indent=1, default=str)
                + "\n\nDirector's revision notes (follow them exactly; rewrite DESCRIPTION "
                "and SETTING_PROMPT to match; update location/time_of_day if they change; "
                "never add people, characters, or text overlays):\n" + notes.strip()
                + '\n\nReturn ONLY JSON: {"id": ..., "name": ..., "description": ..., '
                  '"setting_prompt": ..., "location": ..., "time_of_day": ...}')
        data = self.llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model=model, temperature=0.7)
        if not isinstance(data, dict) or not data:
            # chat_json raises on transport/parse failure, so reaching here
            # with a non-dict means the model ignored the JSON contract: fail
            # loud instead of re-rendering the unchanged brief (same image).
            raise RuntimeError(
                f"scene rewrite for {sid!r} returned no usable brief")
        data["id"] = sid
        if not str(data.get("name") or "").strip():
            data["name"] = str(scene.get("name") or sid)
        if notes.strip() and _brief_text(data) == _brief_text(scene):
            # The model echoed the brief and ignored the revision notes:
            # rendering from this would return the same location again.
            # Block the retake with the cause instead of burning a render.
            raise RuntimeError(
                f"scene rewrite for {sid!r} ignored the revision notes; "
                f"brief unchanged")
        with _show_lock(self.show.show_id):
            self.show.write_scene(data)
        return data

    def bootstrap_scene_ref(self, scene: dict[str, Any], sid: str,
                            force: bool = False, notes: str = "") -> bool:
        """Generate a location reference image (krea2) for one scene.

        Returns True when an image landed (or a stub was recorded so reconcile
        stops retrying). A stub refs.json prevents an infinite reconcile loop
        when no krea2 instance is configured or generation is unavailable.
        When notes ask for a different *place*, the yaml is rewritten first —
        appended prompt notes alone cannot beat the base description.
        """
        from . import scenes
        if notes.strip():
            scene = self.refresh_scene(sid, notes)
        return scenes.generate_ref(self.show, scene, sid, cfg=self.cfg,
                                   report=self._report, force=force,
                                   notes=notes)


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").strip().lower()).strip("-")


def _brief_text(scene: dict[str, Any]) -> str:
    """Normalized description + setting_prompt for rewrite change detection."""
    import re as _re
    text = f"{scene.get('description') or ''}\n{scene.get('setting_prompt') or ''}"
    return _re.sub(r"\s+", " ", text).strip().lower()


def _pitch_slug(text: str) -> str:
    """Normalize a pitch's proposed show id (or title) to a folder-safe slug."""
    return _slug(text or "")[:40].strip("-")