"""Prose-first episode pipeline: whole episode as free text, then shots by need.

Flow the user asked for:

  1. WRITE  — one LLM call writes the WHOLE episode as a plain-text screenplay
     (``episode_prose.txt``). No JSON, no shot ids, no durations.
  2. REVIEW — the prose is reviewed as story. Reviewers still run through the
     existing writers'-room prompts via a lightweight pseudo-script derived
     deterministically from the prose (scene headers + paragraphs + dialogue),
     so all 11 reviewers work unchanged. Revision is prose-to-prose.
  3. BREAK  — the reviewed prose is cut into shots based on what each beat
     needs: dialogue density sets coverage, action paragraphs set inserts, and
     the existing technical passes (blocking from prose, then camera/action/
     references/costumes/soundscape) fill the machine fields the renderer
     requires. Shots JSON is still produced at the end — it has to be, the
     renderer reads it — but no creative call ever sees a schema first.

Resumable: runs/EP##/episode_prose.txt (+ .rN revisions) persist the story;
scenes/<sid>_shots.json checkpoints persist the breakdown as today.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import prompts
from . import promptlog
from .bootstrap import ACTIVITY
from .config import get_config
from .show import Show

PROSE_TXT = "episode_prose.txt"
PROSE_INDEX = "episode_prose.json"   # deterministic parse cache, not LLM output

_HEADER_RE = re.compile(
    r"^===\s*(s\d+)(?:\s*\|\s*(.*?))?\s*===\s*$", re.IGNORECASE)


def _prose_path(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / PROSE_TXT


def _prose_index_path(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / PROSE_INDEX


def parse_prose_scenes(text: str) -> list[dict[str, Any]]:
    """Split a prose screenplay into scenes (deterministic, no LLM).

    Header: ``=== s01 | Location | Name, Name ===`` (location/cast optional).
    Body: action paragraphs + ``NAME: line`` dialogue + ``(silent)`` marker.
    Returns [{id, location, characters, prose, beats, dialogue:[{char,line,delivery}]}].
    """
    scenes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body_lines: list[str] = []

    def _flush() -> None:
        if not current:
            return
        prose = "\n".join(body_lines).strip()
        paras = [p.strip() for p in re.split(r"\n\s*\n", prose) if p.strip()]
        beats = [p for p in paras if ":" not in p.split("\n")[0][:60] or not
                 re.match(r"^[A-Z][A-Z .'-]{1,30}:", p)]
        # Dialogue: lines shaped NAME: words (same discipline as dialogue spine).
        dialogue: list[dict[str, Any]] = []
        for raw in prose.splitlines():
            line = raw.strip()
            if not line or line.lower() in ("(silent)", "silent"):
                continue
            if ":" not in line:
                continue
            name_part, _, spoken = line.partition(":")
            name_part, spoken = name_part.strip(), spoken.strip()
            if not name_part or not spoken or len(name_part.split()) > 4:
                continue
            if not re.match(r"^[A-Za-z][A-Za-z .'-]*$", name_part):
                continue
            delivery = ""
            m = re.match(r"^\(([^)]*)\)\s*(.*)$", spoken)
            if m:
                delivery, spoken = m.group(1).strip(), m.group(2).strip()
            if not spoken:
                continue
            entry: dict[str, Any] = {"char": name_part, "line": spoken}
            if delivery:
                entry["delivery"] = delivery
            dialogue.append(entry)
        current["prose"] = prose
        current["beats"] = beats or ([prose] if prose else [])
        current["dialogue"] = dialogue
        current["silent"] = (not dialogue and
                             any(l.strip().lower() in ("(silent)", "silent")
                                 for l in body_lines))
        scenes.append(current)

    for raw in (text or "").splitlines():
        m = _HEADER_RE.match(raw.strip())
        if m:
            _flush()
            sid = m.group(1).lower()
            rest = (m.group(2) or "").strip()
            parts = [p.strip() for p in rest.split("|")]
            location = parts[0] if len(parts) >= 1 else ""
            cast = [c.strip() for c in parts[1].split(",")] if len(parts) >= 2 else []
            current = {"id": sid, "location": location,
                       "characters": [c for c in cast if c],
                       "prose": "", "beats": [], "dialogue": []}
            body_lines = []
            continue
        if current is None:
            continue  # preamble before first header
        body_lines.append(raw.rstrip())
    _flush()
    return scenes


def pseudo_script(episode: int, scenes: list[dict[str, Any]],
                  summary: str = "") -> dict[str, Any]:
    """Shape parsed prose as a script so existing reviewers run unchanged.

    Each prose paragraph becomes one placeholder shot (action = paragraph);
    dialogue lines attach to the shot of their paragraph. Reviewers read
    location/summary/narrative/beats/action/dialogue — all present — and never
    touch technical fields, so their notes target story, not schema.
    """
    out_scenes: list[dict[str, Any]] = []
    for sc in scenes:
        shots: list[dict[str, Any]] = []
        for i, para in enumerate(sc.get("beats") or [], start=1):
            shots.append({"id": f"{sc['id']}_sh{i:02d}", "duration_s": 8.0,
                          "action": para[:2000], "dialogue": [],
                          "references": {"characters": sc.get("characters", [])}})
        # Attach dialogue lines round-robin to shots so voice/slop reviewers see them.
        dlg = sc.get("dialogue") or []
        for k, d in enumerate(dlg):
            if shots:
                shots[k % len(shots)]["dialogue"].append(d)
        out_scenes.append({"id": sc.get("id"), "location": sc.get("location", ""),
                           "summary": (sc.get("beats") or [""])[0][:300],
                           "narrative": sc.get("prose", ""),
                           "beats": sc.get("beats", []),
                           "characters": sc.get("characters", []),
                           "shots": shots})
    return {"episode": episode, "summary": summary, "cast": [],
            "scenes": out_scenes}


def read_episode_prose(show: Show, episode: int) -> str:
    p = _prose_path(show, episode)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_prose_scenes(show: Show, episode: int) -> list[dict[str, Any]]:
    p = _prose_index_path(show, episode)
    if not p.exists():
        return parse_prose_scenes(read_episode_prose(show, episode))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        scenes = data.get("scenes")
        return scenes if isinstance(scenes, list) else []
    except Exception:
        return []


def generate_episode_prose(show: Show, episode: int, llm=None, cfg=None,
                           notes: str = "") -> str:
    """Stage P1: write the whole episode as free prose (one chat call, no JSON)."""
    from .clients.lmstudio import LMStudioClient
    from .planner import _target, read_director_notes, read_episode_plan

    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=600)
    model = (cfg.get("llm", "roles", {}).get("script")
             or cfg.get("llm", "roles", {}).get("showrunner")
             or cfg.get("llm", "model"))
    bible = show.read_bible()
    continuity = show.read_continuity()
    characters = [show.read_character(cid) for cid in show.list_characters()]
    plan = read_episode_plan(show, episode)
    skeleton = plan.get("scenes", []) if isinstance(plan, dict) else []
    ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writing whole-episode prose…",
                              "ts": time.time()}
    text = promptlog.logged_chat(
        llm, show, episode, "prose-draft",
        [{"role": "system", "content": prompts.showrunner_system(
            show.style_profile(cfg), bible.get("content_policy", "mature"),
            show.style_profile(cfg).get("baseline", ""))},
         {"role": "user", "content": prompts.episode_prose_prompt(
             bible, skeleton, characters, continuity, _target(show),
             read_director_notes(show), notes)}],
        model=model, role="script", temperature=0.8, max_tokens=16384,
        on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
            "detail": f"Episode {episode}: episode prose ({n} tokens…)",
            "output": t[-500:], "ts": time.time()}}))
    text = (text or "").strip()
    if not text:
        raise RuntimeError("episode prose generation returned empty text")
    runs = show.dir / "runs" / f"EP{episode:02d}"
    runs.mkdir(parents=True, exist_ok=True)
    _prose_path(show, episode).write_text(text, encoding="utf-8")
    scenes = parse_prose_scenes(text)
    _prose_index_path(show, episode).write_text(
        json.dumps({"episode": episode, "scenes": scenes}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return text


def review_episode_prose(show: Show, episode: int, prose: str,
                         llm=None, cfg=None, round_no: int = 1,
                         notes_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Stage P2: review the prose as story via existing reviewers (pseudo-script)."""
    from .review import run_reviewers

    cfg = cfg or get_config()
    scenes = parse_prose_scenes(prose)
    plan = None
    try:
        from .planner import read_episode_plan
        plan = read_episode_plan(show, episode)
    except Exception:
        plan = {}
    summary = (plan or {}).get("plot") or (plan or {}).get("summary", "")
    pseudo = pseudo_script(episode, scenes, summary)
    base = notes_dir or (show.dir / "runs" / f"EP{episode:02d}" / "reviews")
    # Tag prose rounds distinctly (.prN) so they never collide with shot-script (.rN).
    results = run_reviewers(pseudo, show=show, cfg=cfg, llm=llm, episode=episode,
                            round_no=round_no, notes_dir=base)
    # Rename *.rN.json -> *.prN.json for this round.
    for f in base.glob(f"*.r{round_no}.json"):
        f.rename(f.with_name(f.name.replace(f".r{round_no}.json", f".pr{round_no}.json")))
    return results


def revise_episode_prose(prose: str, reviews: dict[str, Any],
                         llm=None, cfg=None, show=None, episode: int = 0) -> str:
    """Stage P2b: prose-to-prose revision (chat, no JSON either side)."""
    from .clients.lmstudio import LMStudioClient

    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=600)
    model = (cfg.get("llm", "roles", {}).get("script")
             or cfg.get("llm", "roles", {}).get("showrunner")
             or cfg.get("llm", "model"))
    text = promptlog.logged_chat(
        llm, show, episode, "prose-revise",
        [{"role": "system", "content": prompts.showrunner_system(
            show.style_profile(cfg), "mature", show.style_profile(cfg).get("baseline", ""))},
         {"role": "user", "content": prompts.prose_revision_prompt(prose, reviews)}],
        model=model, role="script", temperature=0.7, max_tokens=16384)
    return (text or "").strip() or prose


def write_prose_review_loop(show: Show, episode: int, prose: str,
                            llm=None, cfg=None,
                            max_revisions: int = 2) -> tuple[str, dict[str, Any]]:
    """Review prose, revise prose, re-review — up to max_revisions. Returns (prose, reviews)."""
    from .review import all_pass

    cfg = cfg or get_config()
    max_revisions = max_revisions or int(cfg.get("reviewers", "max_revisions", 2))
    round_no = 1
    reviews = review_episode_prose(show, episode, prose, llm=llm, cfg=cfg,
                                   round_no=round_no)
    while not all_pass(reviews) and round_no < max_revisions:
        round_no += 1
        ACTIVITY[show.show_id] = {
            "detail": f"Episode {episode}: prose revision (round {round_no})…", "ts": time.time()}
        prose = revise_episode_prose(prose, reviews, llm=llm, cfg=cfg,
                                       show=show, episode=episode)
        _prose_path(show, episode).write_text(prose, encoding="utf-8")
        _prose_index_path(show, episode).write_text(
            json.dumps({"episode": episode, "scenes": parse_prose_scenes(prose)},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        reviews = review_episode_prose(show, episode, prose, llm=llm, cfg=cfg,
                                       round_no=round_no)
    persist_prose_nickname_ledger(show, episode, prose)
    return prose, reviews


def breakdown_prose_to_shots(show: Show, episode: int, llm=None, cfg=None,
                             notes: str = "") -> dict[str, Any]:
    """Stage P3: cut reviewed prose into shots by need, then technical passes.

    Per parsed scene: the blocking pass segments prose beats into shots sized by
    content (dialogue density ~2 lines/10s, silent inserts ~5s); the remaining
    passes fill camera/action/references/costumes/soundscape exactly as today.
    Dialogue comes from the prose itself (verbatim spine), so no per-scene
    dialogue writer runs.
    """
    import json as _json
    from .planner import _scenes_dir, generate_scene_shots, read_episode_plan

    cfg = cfg or get_config()
    scenes = read_prose_scenes(show, episode)
    if not scenes:
        raise RuntimeError("no parsed prose scenes to break down — write prose first")
    plan = read_episode_plan(show, episode)
    ep_summary = (plan or {}).get("plot") or (plan or {}).get("summary", "")
    sc_dir = _scenes_dir(show, episode)
    sc_dir.mkdir(parents=True, exist_ok=True)
    episodes: list[dict[str, Any]] = []
    for ps in scenes:
        sid = ps.get("id", "s01")
        ck = sc_dir / f"{sid}_shots.json"
        if ck.exists():
            try:
                scene = _json.loads(ck.read_text(encoding="utf-8"))
                if scene.get("id") and scene.get("shots"):
                    episodes.append(scene)
                    continue
            except Exception:
                pass
        plan_scene = {"id": sid, "location": ps.get("location", ""),
                      "summary": (ps.get("beats") or [""])[0][:300],
                      "narrative": ps.get("prose", ""),
                      "characters": ps.get("characters", [])}
        spine = ps.get("dialogue") or None
        scene = generate_scene_shots(show, episode, plan_scene, llm=llm, cfg=cfg,
                                     episode_summary=ep_summary, notes=notes,
                                     dialogue_spine=spine)
        ck.write_text(_json.dumps(scene, indent=2, ensure_ascii=False), encoding="utf-8")
        episodes.append(scene)
    return {"episode": episode, "summary": ep_summary, "scenes": episodes}


def write_prose_dialogue_spine(show: Show, episode: int,
                               scenes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Persist the prose's dialogue as dialogue.json + dialogue.txt.

    dialogue.json (cast-resolved, verbatim) is the spine the shot blocker
    distributes; dialogue.txt is the human-readable script the dashboard shows.
    """
    from .planner import _cast_lookup, _dialogue_path, DIALOGUE_TXT

    characters = [show.read_character(cid) for cid in show.list_characters()]
    lut = _cast_lookup([c.get("name") for c in characters if c.get("name")])
    spine: dict[str, list[dict[str, Any]]] = {}
    txt_lines: list[str] = []
    for sc in scenes:
        lines: list[dict[str, Any]] = []
        for d in sc.get("dialogue") or []:
            entry = dict(d)
            entry["char"] = lut.get(str(d.get("char", "")).lower(),
                                    d.get("char", ""))
            lines.append(entry)
        if lines or sc.get("silent"):
            spine[sc.get("id", "s01")] = lines
        if sc.get("id"):
            txt_lines.append(f"=== {sc['id']} ===")
            if sc.get("silent") and not lines:
                txt_lines.append("(silent)")
            for entry in lines:
                delivery = f"({entry['delivery']}) " if entry.get("delivery") else ""
                txt_lines.append(f"{entry['char']}: {delivery}{entry['line']}")
            txt_lines.append("")
    p = _dialogue_path(show, episode)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"episode": episode, "scenes": spine}, indent=2,
                            ensure_ascii=False), encoding="utf-8")
    (p.parent / DIALOGUE_TXT).write_text("\n".join(txt_lines).strip() + "\n",
                                         encoding="utf-8")
    return spine


def persist_prose_nickname_ledger(show: Show, episode: int, prose: str) -> None:
    """Union the prose's address terms into continuity (never replaces).

    Preserves the old whole-episode dialogue writer's ledger behavior on the
    single path: later episodes reuse the SAME terms for the same people.
    """
    from .planner import merge_nickname_ledger, parse_nickname_ledger

    try:
        characters = [show.read_character(cid) for cid in show.list_characters()]
        cast_names = [c.get("name") for c in characters if c.get("name")]
        ledger = parse_nickname_ledger(prose, cast_names)
        if ledger:
            state = show.read_continuity()
            state["nicknames"] = merge_nickname_ledger(state.get("nicknames"), ledger)
            show.write_continuity(state)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "nickname ledger persist failed for %s EP%02d: %s", show.show_id, episode, exc)


def prose_assembly_scenes(show: Show, episode: int, llm=None, cfg=None,
                          notes: str = "") -> list[dict[str, Any]] | None:
    """Prose-first script source for assemble_episode_script.

    Writes the whole episode as free prose (or revises the existing draft when
    director feedback is present), reviews it as story, persists the dialogue
    spine, and returns plan_scene dicts built FROM the reviewed prose
    (narrative = prose) so the shot blocker cuts shots by need. Returns None
    when no usable prose scenes result — the caller falls back to the legacy
    outline/treatment path.
    """
    from .review import all_pass

    cfg = cfg or get_config()
    existing = read_episode_prose(show, episode)
    if existing and not notes.strip() and read_prose_scenes(show, episode):
        prose = existing
    elif existing and notes.strip():
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: revising prose "
                                  "from director feedback…", "ts": time.time()}
        prose = revise_episode_prose(existing, {"director": {
            "pass": False,
            "notes": [{"severity": "HIGH", "note": notes}]}}, llm=llm, cfg=cfg,
            show=show, episode=episode)
        _prose_path(show, episode).write_text(prose, encoding="utf-8")
        _prose_index_path(show, episode).write_text(
            json.dumps({"episode": episode, "scenes": parse_prose_scenes(prose)},
                       indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        prose = generate_episode_prose(show, episode, llm=llm, cfg=cfg, notes=notes)
    prose, reviews = write_prose_review_loop(show, episode, prose, llm=llm, cfg=cfg)
    if not all_pass(reviews):
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "episode prose still failing review; breaking down best draft anyway")
    scenes = parse_prose_scenes(prose)
    if not scenes:
        return None
    write_prose_dialogue_spine(show, episode, scenes)
    return [{"id": sc.get("id", f"s{i:02d}"),
             "location": sc.get("location", ""), "time_of_day": "",
             "summary": (sc.get("beats") or [""])[0][:300],
             "narrative": sc.get("prose", ""),
             "characters": sc.get("characters", [])}
            for i, sc in enumerate(scenes, start=1)]
