"""Board supervisor: expands milestones, launches one agent per free lane.

The daemon is the only thing that moves a card between phases. It never does
creative work itself; it reads the agent's final verdict, applies the
transition, and fills a free lane with the next eligible card.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import get_config
from .agent import Agent, write_trace
from .roles import lane_config, load_roles
from .templates import shot_cards

log = logging.getLogger(__name__)

AgentFactory = Callable[[Any, dict[str, Any], str, str], Any]

#: Lanes that put a model on the GPU. Only one of these runs at a time.
GPU_LANES = frozenset({"llm", "image", "video", "audio", "vision"})


class BoardDaemon:
    def __init__(self, store, cfg=None, *, tick_s: float = 5.0,
                 agent_factory: AgentFactory | None = None,
                 run_timeout_s: float = 4 * 3600):
        self.store = store
        self.cfg = cfg or get_config()
        self.tick_s = float(tick_s)
        self.run_timeout_s = float(run_timeout_s)
        self.agent_factory = agent_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._threads: dict[str, threading.Thread] = {}

    # ---- lifecycle ----
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="board-daemon",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                for action in self.tick():
                    log.info("[board] %s", action)
            except Exception:
                log.exception("[board] tick failed")

    # ---- one pass ----
    def tick(self) -> list[str]:
        actions: list[str] = []
        active = {rid for rid, thread in list(self._threads.items())
                  if thread.is_alive()}
        for run_id in self.store.recover_missing(active):
            actions.append(f"recovered lost worker {run_id}")
        for run_id in self.store.recover_stale(self.run_timeout_s):
            actions.append(f"recovered stale run {run_id}")
        actions += self._block_unknown_roles()
        actions += self._expand_render()
        actions += self._requeue_retakes()
        actions += self._route_verified()
        actions += self._expand_outline()
        actions += self._rollup()
        actions += self._launch()
        return actions

    # ---- validation ----
    def _block_unknown_roles(self) -> list[str]:
        roles = load_roles(self.cfg)
        actions: list[str] = []
        for card in self.store.all():
            if card.get("state") not in ("planned", "agent_verified"):
                continue
            wanted = (str(card.get("reviewer") or "") if card.get("state") == "agent_verified"
                      else str(card.get("role") or ""))
            if wanted and wanted not in roles:
                self.store.note(card["id"], "daemon", f"unknown role {wanted!r}")
                self.store.advance(card["id"], "blocked", "daemon",
                                   f"unknown role {wanted!r}")
                actions.append(f"blocked {card['id']} (unknown role {wanted})")
        return actions

    # ---- expansion ----
    def _expand_render(self) -> list[str]:
        actions: list[str] = []
        cards = self.store.all()
        for card in [c for c in cards if c.get("state") == "planned"]:
            if card.get("kind") != "render":
                continue
            if not self._deps_ok(card, cards):
                continue
            children = [c for c in self.store.all(show_id=card.get("show_id"),
                                                  episode=card.get("episode"))
                        if (c.get("payload") or {}).get("parent") == card["id"]]
            if children:
                continue
            from ..render import _latest_script
            from ..show import Show
            try:
                show = Show(card["show_id"])
                script = _latest_script(show, int(card.get("episode") or 0)) or {}
            except Exception as exc:
                self.store.note(card["id"], "daemon", f"cannot read script: {exc}")
                self.store.advance(card["id"], "blocked", "daemon", str(exc))
                actions.append(f"blocked {card['id']} (no script)")
                continue
            shot_ids = [s.get("id") for sc in script.get("scenes") or []
                        for s in sc.get("shots") or [] if s.get("id")]
            if not shot_ids:
                self.store.note(card["id"], "daemon", "script has no shots")
                self.store.advance(card["id"], "blocked", "daemon", "script has no shots")
                actions.append(f"blocked {card['id']} (no shots)")
                continue
            for child in shot_cards(card["show_id"], int(card.get("episode") or 0),
                                    card["id"], shot_ids):
                self.store.upsert(child)
            self.store.update(card["id"], units=[
                {"id": sid, "label": sid, "done": False} for sid in shot_ids])
            self.store.advance(card["id"], "in_work", "daemon", "expanded into shot cards")
            actions.append(f"expanded {card['id']} -> {len(shot_ids)} shot(s)")
        return actions

    # ---- outline expansion ----
    def _expand_outline(self) -> list[str]:
        """Turn an accepted skeleton into one beats card per batch + the merge.

        The skeleton decides how many scenes exist, so it must run before the
        beats can be split. The merge card is created by the template in
        ``inbox`` and only becomes launchable here, after its beat dependencies
        exist.
        """
        from .cards import new_card
        from .templates import episode_id, show_card_id
        from ..planner import read_plan_skeleton
        from ..show import Show

        actions: list[str] = []
        cards = self.store.all()
        batch_size = max(1, int(self.cfg.get("pipeline", "plan_beats_batch", 2) or 2))
        for card in cards:
            if card.get("kind") != "outline_skeleton" or card.get("state") != "accepted":
                continue
            show_id = str(card.get("show_id") or "")
            ep = int(card.get("episode") or 0)
            epx = show_card_id(show_id, episode_id(ep))
            kids = [c for c in cards
                    if (c.get("payload") or {}).get("parent") == card["id"]
                    and c.get("kind") == "outline_beats"]
            if kids:
                continue
            try:
                skeleton = read_plan_skeleton(Show(show_id), ep)
                scene_ids = [str(s.get("id")) for s in (skeleton.get("scenes") or [])
                             if isinstance(s, dict) and s.get("id")]
                if not scene_ids:
                    raise RuntimeError("skeleton has no scenes")
                made: list[str] = []
                for n, i in enumerate(range(0, len(scene_ids), batch_size), start=1):
                    ids = scene_ids[i:i + batch_size]
                    cid = f"{epx}.outline.beats.{n:02d}"
                    self.store.upsert(new_card(
                        cid, kind="outline_beats",
                        title=f"EP{ep:02d} outline beats {ids[0]}..{ids[-1]}",
                        show_id=show_id, episode=ep, role="outline_writer",
                        lane="llm", priority=5,
                        payload={"scene_ids": ids, "batch": n,
                                 "parent": card["id"],
                                 "tool": "outline_beats", "args": {}}))
                    made.append(cid)
                merge_id = f"{epx}.outline"
                merge = self.store.get(merge_id)
                if not merge:
                    merge = self.store.create(new_card(
                        merge_id, kind="outline",
                        title=f"Write the EP{ep:02d} outline",
                        show_id=show_id, episode=ep, role="outline_writer_agent",
                        reviewer="outline_reviewer", lane="llm", priority=5,
                        payload={"tool": "outline_merge", "args": {}},
                        state="inbox", depends_on=made))
                else:
                    self.store.update(merge_id, depends_on=made)
                    if merge.get("state") == "inbox":
                        self.store.advance(merge_id, "planned", "daemon",
                                           f"expanded into {len(made)} beats card(s)")
                actions.append(f"expanded {card['id']} -> {len(made)} beats card(s)")
            except Exception as exc:
                self.store.note(card["id"], "daemon",
                                f"outline expand failed: {exc}")
                merge = self.store.get(f"{epx}.outline")
                if merge and merge.get("state") == "inbox":
                    self.store.advance(merge["id"], "blocked", "daemon",
                                       f"outline expand failed: {exc}")
                actions.append(f"blocked {epx}.outline (expand failed)")
        return actions

    # ---- retakes ----
    def _requeue_retakes(self) -> list[str]:
        actions: list[str] = []
        for card in self.store.all(state="retake"):
            if int(card.get("attempts") or 0) < int(card.get("max_attempts") or 2):
                self.store.advance(card["id"], "planned", "daemon", "retake")
                actions.append(f"requeued {card['id']}")
            else:
                self.store.advance(card["id"], "blocked", "daemon", "retake cap")
                actions.append(f"blocked {card['id']} (retake cap)")
        return actions

    # ---- review routing ----
    def _route_verified(self) -> list[str]:
        """Accept verified cards that have no reviewer. Reviewed cards stay
        agent_verified until _launch claims them into the reviewer phase."""
        roles = load_roles(self.cfg)
        actions: list[str] = []
        for card in self.store.all(state="agent_verified"):
            reviewer = str(card.get("reviewer") or "")
            if reviewer == "human":
                self.store.advance(card["id"], "reviewing", "daemon", "awaiting human")
                actions.append(f"human review {card['id']}")
                continue
            if reviewer and reviewer in roles:
                continue
            self.store.advance(card["id"], "accepted", "daemon", "no reviewer")
            actions.append(f"accepted {card['id']}")
        return actions

    # ---- milestone rollup ----
    def _rollup(self) -> list[str]:
        actions: list[str] = []
        for card in self.store.all(state="in_work"):
            if card.get("kind") != "render":
                continue
            children = [c for c in self.store.all(show_id=card.get("show_id"),
                                                  episode=card.get("episode"))
                        if (c.get("payload") or {}).get("parent") == card["id"]]
            if not children or any(c.get("state") != "accepted" for c in children):
                continue
            units = [dict(u, done=True) for u in card.get("units") or []]
            self.store.update(card["id"], units=units)
            self.store.advance(card["id"], "agent_verified", "daemon", "all shots accepted")
            self.store.advance(card["id"], "accepted", "daemon", "rollup")
            actions.append(f"accepted milestone {card['id']}")
        return actions

    # ---- launch ----
    def _launch(self) -> list[str]:
        roles = load_roles(self.cfg)
        lanes = lane_config(self.cfg)
        actions: list[str] = []
        cards = self.store.all()
        for lane, spec in lanes.items():
            gpu_lane = lane in GPU_LANES
            if gpu_lane and self._gpu_busy():
                continue
            free = int(spec.get("capacity", 1)) - self.store.lane_load(lane)
            if gpu_lane:
                free = min(free, 1)
            if free <= 0:
                continue
            candidates: list[tuple[dict[str, Any], str, str]] = []
            for card in cards:
                if card.get("state") == "planned" and card.get("kind") != "render":
                    role_name = str(card.get("role") or "")
                    role = roles.get(role_name)
                    if role and not role.is_reviewer and role.lane == lane:
                        candidates.append((card, role_name,
                                           "exec" if (role.is_exec or role.run_as_exec)
                                           else "produce"))
                elif card.get("state") == "agent_verified":
                    role_name = str(card.get("reviewer") or "")
                    role = roles.get(role_name)
                    if role and role.is_reviewer and role.lane == lane and role_name != "human":
                        candidates.append((card, role_name, "review"))
            candidates.sort(key=lambda item: (
                -int(item[0].get("priority") or 0),
                int(item[0].get("episode") or 0),
                item[0].get("created") or ""))
            for card, role_name, phase in candidates:
                if free <= 0:
                    break
                if not self._deps_ok(card, cards):
                    continue
                if phase in ("produce", "exec") and int(card.get("attempts") or 0) >= int(card.get("max_attempts") or 2):
                    self.store.advance(card["id"], "blocked", "daemon", "retake cap")
                    actions.append(f"blocked {card['id']} (retake cap)")
                    continue
                role = roles[role_name]
                run_id = f"run_{card['id']}_{int(time.time() * 1000)}"
                expected = "planned" if phase in ("produce", "exec") else "agent_verified"
                claimed = "in_work" if phase in ("produce", "exec") else "reviewing"
                if not self.store.claim(card["id"], run_id, expected, claimed):
                    continue
                self.store.start_run(run_id, card["id"], role_name, phase,
                                     role.lane, role.model)
                self._spawn(run_id, card, role, phase)
                free -= 1
                actions.append(f"{role_name} -> {card['id']} ({run_id})")
                if gpu_lane:
                    break
        return actions

    def _deps_ok(self, card: dict[str, Any], cards: list[dict[str, Any]]) -> bool:
        by_id = {c["id"]: c for c in cards}
        for dep in card.get("depends_on") or []:
            if (by_id.get(dep) or {}).get("state") == "accepted":
                continue
            # A rejected/superseded stage card blocks nothing when a sibling
            # revision of the same stage (same show, same kind, e.g.
            # g0.<show>.bible.r<ts>) was accepted instead. Without this,
            # any stage that went through a revision would pin its
            # downstream cards on the dead original forever.
            dep_kind = (by_id.get(dep) or {}).get("kind") or ""
            show_id = str(card.get("show_id") or "")
            if dep_kind and show_id and any(
                    c.get("show_id") == show_id
                    and c.get("kind") == dep_kind
                    and c.get("state") == "accepted"
                    for c in cards):
                continue
            return False
        return True

    # ---- execution ----
    def _gpu_busy(self) -> bool:
        for run in self.store.runs():
            if run.get("state") != "running" or run.get("lane") not in GPU_LANES:
                continue
            card = self.store.get(run.get("card_id") or "")
            if card and card.get("claimed_by") == run["id"]:
                return True
        return False

    def _spawn(self, run_id: str, card: dict[str, Any], role, phase: str) -> None:
        if phase == "exec" or (phase == "review" and role.run_as_exec):
            target, args = self._execute_exec, (run_id, card, role)
        else:
            target, args = self._execute, (run_id, card, role, phase)
        thread = threading.Thread(target=target, args=args, name=run_id, daemon=True)
        self._threads[run_id] = thread
        thread.start()

    def _create_qc_fix(self, card: dict[str, Any], result: dict[str, Any]) -> None:
        from .cards import new_card
        from .templates import show_card_id
        ep = int(card.get("episode") or 0)
        show_id = str(card.get("show_id") or "")
        fix_id = f"{card['id']}.fix.{int(time.time() * 1000)}"
        self.store.upsert(new_card(
            fix_id, kind="fix", title=f"EP{ep:02d} QC fix",
            show_id=show_id, episode=ep,
            role="fixer", lane="llm", priority=4, max_attempts=2,
            payload={"failures": result.get("failures") or [],
                     "qc_card": card["id"],
                     "render_milestone": show_card_id(show_id,
                                                      f"ep{ep:02d}.render")},
        ))
        self.store.note(card["id"], "daemon", f"created fix card {fix_id}")

    def _execute_exec(self, run_id: str, card: dict[str, Any], role) -> None:
        """Run one tool card directly in the daemon (no opencode worker LLM)."""
        from . import tools as tools_mod
        from ..clients.lmstudio import LMStudioClient
        try:
            run = self.store.run(run_id) or {}
            phase = str(run.get("phase") or "produce")
            is_review = phase == "review" and role.is_reviewer
            payload_spec = card.get("payload") or {}
            if is_review:
                # The reviewer's own tool, not the card's producer tool.
                name = str(role.tools[0] if role.tools else "")
                args: dict[str, Any] = {}
            else:
                kind_tool = {"outline_skeleton": "outline_skeleton",
                             "outline_beats": "outline_beats",
                             "outline": "outline_merge",
                             "outline_scene": "rewrite_scene"}.get(
                                 str(card.get("kind") or ""))
                name = str(payload_spec.get("tool") or kind_tool
                           or (role.tools[0] if role.tools else ""))
                args = payload_spec.get("args") or {}
            llm = None
            if is_review or role.model or getattr(role, "work_model", ""):
                llm = LMStudioClient(self.cfg.get("llm", "base_url"),
                                     timeout=role.timeout_s)
            ctx = tools_mod.ToolContext(store=self.store, card=card, role=role,
                                        phase=phase, run_id=run_id,
                                        llm=llm, cfg=self.cfg)
            result = tools_mod.call(ctx, name, args)

            if is_review:
                passed = bool((result or {}).get("pass"))
                notes = str((result or {}).get("notes") or "")
                final = {"status": "verified" if passed else "retake",
                         "verdict": "pass" if passed else "fail",
                         "summary": name, "notes": notes}
                self.store.finish_run(run_id, "done", "",
                                      {"result": result, "tool": name,
                                       "final": final})
                current = self.store.get(card["id"]) or {}
                merged = dict(current.get("result") or {})
                merged.update({"tool": name, "value": result})
                self.store.update(card["id"], result=merged)
                current = self.store.get(card["id"]) or {}
                if current.get("claimed_by") != run_id:
                    return
                if passed:
                    if (current.get("payload") or {}).get("human_gate"):
                        self.store.advance(card["id"], "reviewing", run_id,
                                            notes)
                    else:
                        self.store.advance(card["id"], "accepted", run_id,
                                            notes)
                else:
                    if notes:
                        self.store.note(card["id"], run_id, notes)
                    self.store.update(
                        card["id"],
                        attempts=int(current.get("attempts") or 0) + 1)
                    self.store.advance(card["id"], "retake", run_id, notes)
                return

            failures = (result or {}).get("failures") if isinstance(result, dict) else None
            status = "blocked" if failures else "verified"
            notes = (f"{len(failures)} shot(s) failed QC" if failures else "")
            if failures:
                self._create_qc_fix(card, result)
            final = {"status": status, "summary": name, "notes": notes}
            self.store.finish_run(run_id, "done", "",
                                  {"result": result, "tool": name,
                                   "final": final})
            current = self.store.get(card["id"]) or {}
            merged = dict(current.get("result") or {})
            merged.update({"tool": name, "value": result})
            self.store.update(card["id"], result=merged)
            current = self.store.get(card["id"]) or {}
            if current.get("claimed_by") != run_id:
                return
            self.store.advance(card["id"],
                               "agent_verified" if status == "verified" else "blocked",
                               run_id, notes or name)
        except Exception as exc:
            log.exception("[board] exec %s failed", run_id)
            self.store.finish_run(run_id, "failed", str(exc))
            self._block(run_id, card, str(exc))
        finally:
            self._threads.pop(run_id, None)

    def _execute(self, run_id: str, card: dict[str, Any], role, phase: str) -> None:
        try:
            agent = (self.agent_factory(role, card, phase, run_id)
                     if self.agent_factory else self._default_agent(role, card, phase, run_id))
            result = agent.run()
            trace_dir = write_trace(self.cfg, run_id,
                                    getattr(agent, "system_prompt", lambda: "")(),
                                    result)
            try:
                trace_rel = (Path(trace_dir).resolve()
                             .relative_to(Path(self.cfg.root).resolve()).as_posix())
            except ValueError:
                trace_rel = str(trace_dir)
            payload = {"status": result.status, "verdict": result.verdict,
                       "summary": result.summary, "notes": result.notes,
                       "trace": trace_rel}
            self.store.finish_run(run_id, "done" if result.ok else "failed",
                                  result.error, payload)
            current = self.store.get(card["id"]) or {}
            merged = dict(current.get("result") or {})
            merged.update(payload)
            self.store.update(card["id"], result=merged)
            self._apply(run_id, card, role, phase, result)
        except Exception as exc:
            log.exception("[board] run %s failed", run_id)
            self.store.finish_run(run_id, "failed", str(exc))
            self._block(run_id, card, str(exc))
        finally:
            self._threads.pop(run_id, None)

    def _default_agent(self, role, card, phase, run_id):
        from ..clients.lmstudio import LMStudioClient
        llm = LMStudioClient(self.cfg.get("llm", "base_url"), timeout=role.timeout_s)
        if (self.cfg.get("agents", "opencode", {}) or {}).get("enabled", True):
            from .opencode import OpenCodeAgent
            return OpenCodeAgent(role=role, card=card, store=self.store,
                                 phase=phase, run_id=run_id, cfg=self.cfg,
                                 llm=llm)
        return Agent(role=role, card=card, store=self.store, phase=phase,
                     run_id=run_id, llm=llm, cfg=self.cfg)

    def _apply(self, run_id: str, card: dict[str, Any], role, phase: str,
               result) -> None:
        current = self.store.get(card["id"]) or {}
        if current.get("claimed_by") != run_id:
            return  # a human or another run already moved it
        if phase == "review":
            if result.verdict == "pass":
                if (current.get("payload") or {}).get("human_gate"):
                    # Gated stage: the agent reviewer passed; a human decides.
                    # (agent_verified -> reviewing is a legal transition.)
                    self.store.advance(card["id"], "reviewing", run_id,
                                        result.notes)
                else:
                    self.store.advance(card["id"], "accepted", run_id,
                                        result.notes)
            else:
                _reason = str(result.notes or result.error or "").strip()
                if _reason:
                    self.store.note(card["id"], run_id, _reason)
                self.store.update(card["id"],
                                  attempts=int(current.get("attempts") or 0) + 1)
                self.store.advance(card["id"], "retake", run_id, _reason)
            return
        if result.status == "verified":
            self.store.advance(card["id"], "agent_verified", run_id, result.summary)
        elif result.status == "retake":
            _reason = str(result.notes or result.error or "").strip()
            if _reason:
                self.store.note(card["id"], run_id, _reason)
            self.store.update(card["id"],
                              attempts=int(current.get("attempts") or 0) + 1)
            self.store.advance(card["id"], "retake", run_id, _reason)
        else:
            self.store.advance(card["id"], "blocked", run_id,
                               result.error or result.notes)

    def _block(self, run_id: str, card: dict[str, Any], reason: str) -> None:
        current = self.store.get(card["id"]) or {}
        if current.get("claimed_by") == run_id and current.get("state") in ("in_work", "reviewing"):
            try:
                self.store.advance(card["id"], "blocked", run_id, reason)
            except ValueError:
                self.store.release(card["id"], run_id)


def request_episode(store, show_id: str, episode: int) -> list[dict[str, Any]]:
    """Idempotently create the card set for one episode."""
    from .templates import episode_cards, episode_id, show_card_id
    epx = episode_id(episode)
    skeleton = show_card_id(show_id, epx, "outline.skeleton")
    has_skeleton = store.get(skeleton) is not None
    out: list[dict[str, Any]] = []
    for card in episode_cards(show_id, episode):
        existing = store.upsert(card)
        if existing and card.get("kind") == "outline":
            if existing.get("role") != "outline_writer_agent":
                existing = store.update(card["id"], role="outline_writer_agent")
        elif (existing and card.get("kind") == "outline_skeleton"
              and existing.get("role") != "outline_writer"):
            existing = store.update(card["id"], role="outline_writer")
        # Migrate an outline card queued before the staged outline existed: it
        # must wait for the skeleton (and later the beats) instead of merging an
        # empty/old plan the moment it is unblocked.
        if (card.get("kind") == "outline" and existing and has_skeleton
                and not any(".beats." in str(d)
                            for d in (existing.get("depends_on") or []))):
            existing = store.update(card["id"], depends_on=[skeleton])
        out.append(existing)
    return out
