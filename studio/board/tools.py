"""Tool registry for board agents.

Tools are the only way an agent touches the studio. Each role gets a
configured allowlist (config/agents.yaml), so a reviewer physically cannot
render and a renderer cannot approve its own card. Handlers are thin wrappers
over the existing executors; the board remains the system of record.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import get_config
from ..show import Show
from .cards import now

TOOLS: dict[str, "Tool"] = {}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: dict[str, str]
    handler: Callable[["ToolContext", dict[str, Any]], Any]
    long: bool = False


@dataclass
class ToolContext:
    store: Any
    card: dict[str, Any]
    role: Any
    phase: str
    run_id: str
    llm: Any = None
    cfg: Any = None
    cancel_event: Any = None

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = get_config()


def tool(name: str, description: str, args: dict[str, str] | None = None,
         long: bool = False):
    def deco(fn: Callable[[ToolContext, dict[str, Any]], Any]):
        TOOLS[name] = Tool(name=name, description=description,
                           args=dict(args or {}), handler=fn, long=long)
        return fn
    return deco


def manifest(role) -> str:
    """Tool list rendered into the agent system prompt."""
    names = list(role.tools)
    if "*" in names:
        names = sorted(TOOLS)
    lines = []
    for name in names:
        spec = TOOLS.get(name)
        if not spec:
            continue
        flags = " ".join(f"--{k.replace('_', '-')} <{v}>"
                         for k, v in spec.args.items()) or "(no args)"
        mark = " [long: --async then board-wait]" if spec.long else ""
        lines.append(f"- {name}: {flags} -> {spec.description}{mark}")
    return "\n".join(lines) or "- (no tools)"


def call(ctx: ToolContext, name: str, args: dict[str, Any]) -> Any:
    spec = TOOLS.get(name)
    if not spec:
        raise KeyError(f"unknown tool {name!r}")
    allowed = list(ctx.role.tools)
    if "*" not in allowed and name not in allowed:
        raise PermissionError(f"role {ctx.role.name!r} may not call {name!r}")
    if not isinstance(args, dict):
        raise TypeError("tool args must be an object")
    return spec.handler(ctx, args)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _show(ctx: ToolContext) -> Show:
    show_id = str(ctx.card.get("show_id") or "")
    if not show_id:
        raise ValueError("card has no show_id")
    return Show(show_id)


def _ep(ctx: ToolContext) -> int:
    ep = int(ctx.card.get("episode") or 0)
    if ep <= 0:
        raise ValueError("card has no episode")
    return ep


def _root(ctx: ToolContext) -> Path:
    return Path(ctx.cfg.root).resolve()


def _rel(ctx: ToolContext, path: Path | str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = _root(ctx) / p
    p = p.resolve()
    try:
        return str(p.relative_to(_root(ctx))).replace("\\", "/")
    except ValueError:
        return str(p)


def _inside(ctx: ToolContext, path: Path | str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _root(ctx) / p
    p = p.resolve()
    root = _root(ctx)
    if p != root and root not in p.parents:
        raise PermissionError(f"path escapes the studio root: {path}")
    return p


def _shot(show: Show, episode: int, sid: str) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    from ..render import _latest_script
    script = _latest_script(show, episode) or {}
    for si, scene in enumerate(script.get("scenes") or [], start=1):
        for hi, shot in enumerate(scene.get("shots") or [], start=1):
            if shot.get("id") == sid:
                return script, scene, si, hi
    raise KeyError(f"shot {sid!r} not found in episode {episode}")


def _attach_unique(ctx: ToolContext, path: Path | str, caption: str = "",
                   asset: dict[str, Any] | None = None) -> None:
    rel = _rel(ctx, path)
    card = ctx.store.get(ctx.card["id"]) or {}
    if any(a.get("path") == rel for a in (card.get("artifacts") or [])):
        return
    ctx.store.attach(ctx.card["id"], rel, caption, asset=asset)


def _clip(show: Show, episode: int, sid: str) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "video" / f"{sid}.mp4"


class _RoleLLM:
    """Force a role's configured model onto executor calls.

    The generators choose their own model from ``llm.roles``; this wrapper makes
    ``agents.roles.<role>.model`` authoritative for the work that role performs,
    while pass-through attributes keep the client's routing/GPU behavior.
    """

    def __init__(self, llm, model: str):
        self._llm = llm
        self._model = model

    def chat(self, messages, model=None, **kwargs):
        return self._llm.chat(messages, model=self._model, **kwargs)

    def chat_json(self, messages, model=None, **kwargs):
        return self._llm.chat_json(messages, model=self._model, **kwargs)

    def __getattr__(self, name):
        return getattr(self._llm, name)


def _role_llm(ctx: ToolContext):
    if ctx.llm is None:
        return None
    return _RoleLLM(ctx.llm, getattr(ctx.role, "work_model", "") or ctx.role.model)


# ---------------------------------------------------------------------------
# board tools
# ---------------------------------------------------------------------------

@tool("read_card", "Read this card, or another card by id.", {"id": "optional card id"})
def read_card(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cid = str(args.get("id") or ctx.card["id"])
    card = ctx.store.get(cid)
    if not card:
        raise KeyError(f"no card {cid!r}")
    return card


@tool("list_cards", "List cards, optionally filtered by state or role.",
      {"state": "optional state", "role": "optional role"})
def list_cards(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cards = ctx.store.all(state=args.get("state"), role=args.get("role"))
    return [{k: c.get(k) for k in ("id", "kind", "title", "state", "role",
                                   "lane", "episode", "attempts")}
            for c in cards]


@tool("add_note", "Append a note to this card.", {"text": "note text"})
def add_note(ctx: ToolContext, args: dict[str, Any]) -> Any:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    ctx.store.note(ctx.card["id"], ctx.run_id, text)
    return {"ok": True}


@tool("attach_artifact", "Attach a produced file as proof for this card.",
      {"path": "repo-relative or absolute path", "caption": "what it proves"})
def attach_artifact(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _inside(ctx, str(args.get("path") or ""))
    if not path.exists():
        raise FileNotFoundError(f"artifact does not exist: {args.get('path')}")
    ctx.store.attach(ctx.card["id"], _rel(ctx, path),
                     caption=str(args.get("caption") or ""),
                     kind=path.suffix.lstrip("."))
    return {"ok": True, "path": _rel(ctx, path)}


@tool("check_units", "Mark countable units of this card done.",
      {"ids": "list of unit ids"})
def check_units(ctx: ToolContext, args: dict[str, Any]) -> Any:
    ids = args.get("ids") or []
    if not isinstance(ids, list):
        raise TypeError("ids must be a list")
    card = ctx.store.check_units(ctx.card["id"], [str(i) for i in ids], ctx.run_id)
    return {"units": card.get("units")}


@tool("read_artifact", "Read a text/JSON artifact from the show's runs.",
      {"path": "repo-relative path", "limit": "max characters (default 12000)"})
def read_artifact(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _inside(ctx, str(args.get("path") or ""))
    limit = int(args.get("limit") or 12000)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
            return data
        except ValueError:
            pass
    return text[:limit]


@tool("read_show", "Read the show canon: bible summary, cast, continuity, episode state.")
def read_show(ctx: ToolContext, args: dict[str, Any]) -> Any:
    show = _show(ctx)
    ep = int(ctx.card.get("episode") or 0)
    bible = show.read_bible() or {}
    continuity = show.read_continuity() or {}
    runs = show.dir / "runs" / f"EP{ep:02d}"
    return {
        "show_id": show.show_id,
        "episode": ep,
        "bible": {k: bible.get(k) for k in
                  ("title", "genre", "tone", "content_policy", "mature_spec",
                   "overall_plotline", "plotlines", "cast")},
        "characters": [c.get("name") for c in
                       (show.read_character(cid) for cid in show.list_characters())
                       if c.get("name")],
        "continuity": {k: continuity.get(k) for k in
                       ("episode", "unresolved_threads", "continuity_rules")},
        "artifacts": {
            "plan": (runs / "plan.json").exists(),
            "script": bool(list(runs.glob("script.r*.json"))),
            "storyboard": (runs / "storyboard").exists(),
            "video": (runs / "video").exists(),
        },
    }


# ---------------------------------------------------------------------------
# Gate 0 tools
# ---------------------------------------------------------------------------

@tool("gate0_plan", "Generate Gate 0 text: concept, bible, proposals, scenes.", long=True)
def gate0_plan(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..bootstrap import BootstrapChain
    from . import gate0
    show = _show(ctx)
    payload = ctx.card.get("payload") or {}
    chain = BootstrapChain(show, cfg=ctx.cfg, llm=_role_llm(ctx))
    if payload.get("auto"):
        chain._auto_override = True
    log = chain.advance_plan(brief=str(payload.get("brief") or ""))
    st = gate0.status(show, "plan")
    path = show.dir / "bootstrap.json"
    if path.exists():
        _attach_unique(ctx, path, "Gate 0 state")
    for rel in ("concept.yaml", "bible.yaml"):
        p = show.dir / rel
        if p.exists():
            _attach_unique(ctx, p, rel)
    for cid in show.list_characters():
        _attach_unique(ctx, show.characters_dir / f"{cid}.yaml", f"character {cid}")
    for sid in show.list_scenes():
        _attach_unique(ctx, show.scenes_dir / f"{sid}.yaml", f"scene {sid}")
    ctx.store.update(ctx.card["id"], result={**st, "log": log})
    return {**st, "log": log}


@tool("gate0_ideas", "Pitch N show ideas off the brief; archive them all.", long=True)
def gate0_ideas(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..bootstrap import BootstrapChain
    from . import gate0
    show = _show(ctx)
    payload = ctx.card.get("payload") or {}
    try:
        n = int(payload.get("n") or 5)
    except (TypeError, ValueError):
        n = 5
    chain = BootstrapChain(show, cfg=ctx.cfg, llm=_role_llm(ctx))
    log = chain.advance_ideas(brief=str(payload.get("brief") or ""), n=n)
    st = gate0.status(show, "ideas")
    if show.ideas_path.exists():
        _attach_unique(ctx, show.ideas_path, "Gate 0 ideas archive")
    ctx.store.update(ctx.card["id"], result={**st, "log": log})
    return {**st, "log": log}


@tool("gate0_bible", "Write the series bible from the approved concept.", long=True)
def gate0_bible(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..bootstrap import BootstrapChain
    from . import gate0
    show = _show(ctx)
    st0 = show.bootstrap_state() or {}
    if (st0.get("concept") or {}).get("status") != "approved":
        raise ValueError("concept is not approved yet; pick a pitch first")
    chain = BootstrapChain(show, cfg=ctx.cfg, llm=_role_llm(ctx))
    log = chain.advance_bible()
    st = gate0.status(show, "bible")
    for rel in ("concept.json", "bible.yaml"):
        p = show.dir / rel
        if p.exists():
            _attach_unique(ctx, p, rel)
    ctx.store.update(ctx.card["id"], result={**st, "log": log})
    return {**st, "log": log}


@tool("gate0_characters", "Propose the cast from the approved bible.", long=True)
def gate0_characters(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..bootstrap import BootstrapChain
    from . import gate0
    show = _show(ctx)
    st0 = show.bootstrap_state() or {}
    if (st0.get("bible") or {}).get("status") != "approved":
        raise ValueError("bible is not approved yet")
    chain = BootstrapChain(show, cfg=ctx.cfg, llm=_role_llm(ctx))
    log = chain.advance_characters()
    st = gate0.status(show, "characters")
    for cid in show.list_characters():
        _attach_unique(ctx, show.characters_dir / f"{cid}.yaml",
                       f"character {cid}")
    ctx.store.update(ctx.card["id"], result={**st, "log": log})
    return {**st, "log": log}


@tool("gate0_scenes", "Build the scene registry from the approved bible.", long=True)
def gate0_scenes(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..bootstrap import BootstrapChain
    from . import gate0
    show = _show(ctx)
    st0 = show.bootstrap_state() or {}
    if (st0.get("bible") or {}).get("status") != "approved":
        raise ValueError("bible is not approved yet")
    chain = BootstrapChain(show, cfg=ctx.cfg, llm=_role_llm(ctx))
    log = chain.advance_scene_registry()
    st = gate0.status(show, "scenes")
    for sid in show.list_scenes():
        _attach_unique(ctx, show.scenes_dir / f"{sid}.yaml", f"scene {sid}")
    ctx.store.update(ctx.card["id"], result={**st, "log": log})
    return {**st, "log": log}


@tool("gate0_images", "Generate character and scene reference images (ComfyUI only).", long=True)
def gate0_images(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..bootstrap import BootstrapChain
    from . import gate0
    show = _show(ctx)
    chain = BootstrapChain(show, cfg=ctx.cfg, llm=None)
    from ..gpu_manager import get_gpu_manager
    gm = get_gpu_manager(ctx.cfg)
    try:
        log = chain.advance_images()
    finally:
        gm.unload_comfy()
    st = gate0.status(show, "images")
    path = show.dir / "bootstrap.json"
    if path.exists():
        _attach_unique(ctx, path, "Gate 0 state")
    from .assets import parse_asset
    for cid in show.list_characters():
        rd = show.character_refs_dir(cid)
        if (rd / "refs.json").exists():
            _attach_unique(ctx, rd / "refs.json", f"{cid} refs")
        for p in sorted(rd.glob("*.png"))[:6]:
            _attach_unique(ctx, p, f"{cid} {p.stem}",
                           asset=parse_asset(show, _rel(ctx, p)))
    for sid in show.list_scenes():
        for p in sorted((show.scenes_dir / sid / "refs").glob("*.png"))[:4]:
            _attach_unique(ctx, p, f"{sid} ref",
                           asset=parse_asset(show, _rel(ctx, p)))
    ctx.store.update(ctx.card["id"], result={**st, "log": log})
    return {**st, "log": log}


@tool("image_retake", "Regenerate one rejected Gate 0 image.", long=True)
def image_retake(ctx: ToolContext, args: dict[str, Any]) -> Any:
    import shutil
    import time
    import yaml

    from .. import refs as refs_mod
    from .. import scenes as scenes_mod
    from . import assets

    show = _show(ctx)
    asset = dict(args.get("asset") or (ctx.card.get("payload") or {}).get("asset") or {})
    if not asset:
        raise ValueError("retake card has no asset")
    notes = str(args.get("notes") or (ctx.card.get("payload") or {}).get("notes") or "")
    parent = str(args.get("parent") or (ctx.card.get("payload") or {}).get("parent") or "")
    path = _inside(ctx, asset.get("path") or "")
    archived = ""
    if path.exists():
        stamp = int(time.time() * 1000)
        dest = path.parent / "rejected" / f"{path.stem}.{stamp}{path.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        archived = _rel(ctx, dest)
        ctx.store.attach(ctx.card["id"], archived, "rejected before retake",
                         asset={**asset, "retakeable": False})
    if asset.get("type") == "scene":
        sid = str(asset.get("id") or "")
        scene_path = show.scenes_dir / f"{sid}.yaml"
        scene = (yaml.safe_load(scene_path.read_text(encoding="utf-8")) or {}
                 if scene_path.exists() else {})
        ok = scenes_mod.generate_ref(show, scene, sid, cfg=ctx.cfg,
                                     force=True, notes=notes)
    else:
        cid = str(asset.get("id") or "")
        label = str(asset.get("label") or "")
        char = show.read_character(cid)
        if label == "nude":
            ok = bool(refs_mod.ensure_nude_ref(
                show, cid, char.get("name") or cid, cfg=ctx.cfg,
                force=True, llm=None, notes=notes))
        elif label == "base":
            ok = refs_mod.regenerate_base(show, cid, char, cfg=ctx.cfg,
                                          llm=None, notes=notes)
        else:
            raise ValueError(f"retake not implemented for {assets.asset_title(asset)}")
    if not ok:
        raise RuntimeError(f"retake failed for {assets.asset_title(asset)}")
    assets.mark_pending(show, asset, notes)
    if parent:
        assets.update_artifact(ctx.store, parent, asset.get("path") or "",
                               status="pending", notes=notes,
                               retake_id=ctx.card["id"])
    ctx.store.attach(ctx.card["id"], _rel(ctx, path), "retake", asset=asset)
    return {"asset": asset, "path": _rel(ctx, path), "archived": archived}


@tool("gate0_voices", "Generate voice samples (TTS only).", long=True)
def gate0_voices(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..bootstrap import BootstrapChain
    from . import gate0
    show = _show(ctx)
    chain = BootstrapChain(show, cfg=ctx.cfg, llm=None)
    from ..gpu_manager import get_gpu_manager
    get_gpu_manager(ctx.cfg).unload_comfy()
    raw_char = (ctx.card.get("payload") or {}).get("char")
    if isinstance(raw_char, dict):
        char = str(raw_char.get("name") or raw_char.get("id") or "")
    else:
        char = str(raw_char or "")
    log = chain.advance_voices(char)
    chain.check_complete()
    st = gate0.status(show, "voices")
    if char:
        entry = next((c for c in st.get("characters", [])
                      if str(c.get("name") or "").lower() == char.lower()), {})
        st["pending"] = (None if entry.get("voice") == "approved"
                         else {"step": "voice", "char": char})
        ch = next((show.read_character(c) for c in show.list_characters()
                   if str((show.read_character(c) or {}).get("name") or "").lower()
                   == char.lower()), {})
        if ch.get("voice_sample_text"):
            st["voice_sample_text"] = ch["voice_sample_text"]
    path = show.dir / "bootstrap.json"
    if path.exists():
        _attach_unique(ctx, path, "Gate 0 state")
    for entry in st.get("characters", []):
        name = str(entry.get("name") or "")
        if char and name.lower() != char.lower():
            continue
        cid = next((c for c in show.list_characters()
                    if str((show.read_character(c) or {}).get("name") or "").lower()
                    == name.lower()), "")
        if not cid:
            continue
        from .assets import parse_asset
        vp = show.dir / "voices" / f"{cid}_voice.yaml"
        if vp.exists():
            _attach_unique(ctx, vp, f"{name} voice config",
                           asset=parse_asset(show, _rel(ctx, vp)))
        wp = show.dir / "assets" / "voice" / f"{cid}_voice.wav"
        if wp.exists():
            _attach_unique(ctx, wp, f"{name} voice sample",
                           asset=parse_asset(show, _rel(ctx, wp)))
    ctx.store.update(ctx.card["id"], result={**st, "log": log})
    return {**st, "log": log}


@tool("write_voice_sample",
      "Write a 70-110 word first-person voice sample for a character.",
      {"char": "character name", "notes": "director notes (optional)"})
def write_voice_sample(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from .. import prompts
    show = _show(ctx)
    name = str(args.get("char") or "").strip()
    notes = str(args.get("notes")
                or (ctx.card.get("payload") or {}).get("notes") or "").strip()
    if not name:
        raise ValueError("char is required")
    char = None
    for cid in show.list_characters():
        c = show.read_character(cid) or {}
        if str(c.get("name") or "").lower() == name.lower():
            char = c
            break
    if not char:
        raise KeyError(f"no character named {name!r}")
    bible = show.read_bible() or {}
    profile = show.style_profile(ctx.cfg)
    system = prompts.showrunner_system(profile, bible.get("content_policy") or "",
                                       profile.get("baseline", ""))
    llm = _role_llm(ctx)
    prompt = prompts.voice_sample_prompt(char, bible)
    if notes:
        prompt = prompt.rstrip() + f"\n\nDirector's notes for this rewrite:\n{notes}"
    data = llm.chat_json(
        [{"role": "system", "content": system},
         {"role": "user", "content": prompt}],
        model=ctx.role.model, temperature=0.8)
    text = str((data or {}).get("voice_sample_text") or "").strip()
    if len(text.split()) < 50:
        raise ValueError("model returned a voice sample that is too short")
    char["voice_sample_text"] = text
    show.write_character(char)
    st = show.bootstrap_state()
    for entry in st.get("characters", []):
        if str(entry.get("name") or "").lower() == name.lower():
            entry["voice"] = ""
    show.set_bootstrap_state(st)
    # Guarantee the TTS follow-up exists even if the caller only queued the
    # text card. The voices card waits for this card to be accepted.
    parent = str((ctx.card.get("payload") or {}).get("parent") or "")
    asset = (ctx.card.get("payload") or {}).get("asset") or {}
    active = [c for c in ctx.store.all(show_id=show.show_id)
              if c.get("kind") == "gate0_voices"
              and str((c.get("payload") or {}).get("char") or "").lower() == name.lower()
              and c.get("state") not in ("accepted", "blocked", "deferred")]
    audio_card = active[0]["id"] if active else ""
    if not active:
        import time as _time
        from .cards import new_card
        cid = f"g0.{show.show_id}.voices.r{int(_time.time())}"
        payload = {"char": name}
        if parent:
            payload.update({"parent": parent, "asset": asset})
        card = new_card(
            cid, kind="gate0_voices", title=f"Record voice sample: {name}",
            show_id=show.show_id, role="gate0_voices", reviewer="human",
            lane="audio", priority=8, max_attempts=2,
            payload=payload, depends_on=[ctx.card["id"]])
        ctx.store.upsert(card)
        audio_card = cid
    elif parent:
        current = ctx.store.get(audio_card) or {}
        payload = dict(current.get("payload") or {})
        payload.update({"parent": parent, "asset": asset})
        ctx.store.update(audio_card, payload=payload)
    _attach_unique(ctx, show.characters_dir / f"{char.get('id') or name}.yaml",
                   f"{name} character sheet")
    result = {"char": name, "words": len(text.split()),
              "voice_sample_text": text, "audio_card": audio_card}
    ctx.store.update(ctx.card["id"], result=result)
    return result


@tool("gate0_status", "Read Gate 0 status and the pending item for a domain.",
      {"domain": "plan|images|voices (optional)"})
def gate0_status(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from . import gate0
    domain = str(args.get("domain") or "") or None
    return gate0.status(_show(ctx), domain)


# ---------------------------------------------------------------------------
# creative tools
# ---------------------------------------------------------------------------

@tool("generate_outline", "Generate (or regenerate) this episode's outline/plan.",
      {"notes": "optional reviewer notes to apply on a rewrite"}, long=True)
def generate_outline(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import generate_episode_plan, read_episode_plan
    show, ep = _show(ctx), _ep(ctx)
    notes = _card_notes(ctx, args)
    generate_episode_plan(show, ep, llm=_role_llm(ctx), cfg=ctx.cfg,
                          notes=notes, review=False,
                          cancel=ctx.cancel_event)
    path = show.dir / "runs" / f"EP{ep:02d}" / "plan.json"
    _attach_unique(ctx, path, "episode outline")
    plan = read_episode_plan(show, ep)
    return {"path": _rel(ctx, path), "plot": plan.get("plot") or plan.get("summary"),
            "scenes": len(plan.get("scenes") or []),
            "review": plan.get("plan_review")}


@tool("outline_skeleton", "Draft the episode outline skeleton: meta + one-line "
      "scene list (phase 1 of the chunked outline).",
      {"notes": "optional revision feedback"}, long=True)
def outline_skeleton(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import generate_plan_skeleton
    show, ep = _show(ctx), _ep(ctx)
    out = generate_plan_skeleton(show, ep, llm=_role_llm(ctx), cfg=ctx.cfg,
                                 notes=str(args.get("notes") or ""),
                                 cancel=ctx.cancel_event)
    _attach_unique(ctx, out["path"], "episode outline skeleton")
    return out


@tool("outline_beats", "Expand one batch of outline scenes into Setup/Change/"
      "Consequence beats (phase 2 of the chunked outline).",
      {"scene_ids": "optional comma list; defaults to this card's payload",
       "notes": "optional revision feedback"}, long=True)
def outline_beats(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import generate_plan_beats
    show, ep = _show(ctx), _ep(ctx)
    ids = args.get("scene_ids") or (ctx.card.get("payload") or {}).get("scene_ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()]
    out = generate_plan_beats(show, ep, list(ids), llm=_role_llm(ctx),
                              cfg=ctx.cfg,
                              notes=str(args.get("notes") or ""),
                              cancel=ctx.cancel_event)
    _attach_unique(ctx, out["path"], "scene beats")
    return out


@tool("outline_merge", "Assemble the staged outline into plan.json. On a retake "
      "the card's reviewer notes are applied as a revision; pass fresh=true to "
      "ignore stale card notes and merge the current skeleton/beats.",
      {"notes": "optional reviewer notes to apply on a rewrite",
       "fresh": "true to ignore the card's stale notes and merge the current skeleton/beats"},
      long=True)
def outline_merge(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import merge_outline_stages
    show, ep = _show(ctx), _ep(ctx)
    fresh = str(args.get("fresh") or "").strip().lower() in ("1", "true", "yes")
    notes = "" if fresh else _card_notes(ctx, args)
    out = merge_outline_stages(show, ep, llm=_role_llm(ctx), cfg=ctx.cfg,
                               notes=notes, cancel=ctx.cancel_event)
    _attach_unique(ctx, out["path"], "episode outline")
    return out


@tool("rewrite_scene", "Rewrite one outline scene (summary + beats) with feedback.",
      {"scene_id": "scene to rewrite", "notes": "feedback to apply"}, long=True)
def rewrite_scene(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import rewrite_plan_scene
    show, ep = _show(ctx), _ep(ctx)
    payload = ctx.card.get("payload") or {}
    scene_id = str(args.get("scene_id") or payload.get("scene_id") or "")
    if not scene_id:
        raise ValueError("no scene_id on the rewrite card")
    notes = str(args.get("notes") or payload.get("notes") or "").strip()
    if not notes:
        notes = _card_notes(ctx, args)
    out = rewrite_plan_scene(show, ep, scene_id, notes, llm=_role_llm(ctx),
                             cfg=ctx.cfg, cancel=ctx.cancel_event)
    _attach_unique(ctx, out["path"], f"scene {scene_id} rewrite")
    return out


def _card_notes(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Tool --notes, else the card's accumulated reviewer notes."""
    notes = str(args.get("notes") or "").strip()
    if notes:
        return notes
    raw = ctx.card.get("notes") or []
    parts = [str(n.get("text") if isinstance(n, dict) else n).strip()
             for n in raw]
    return "\n".join(p for p in parts if p)



@tool("generate_script", "Build scene details, then assemble the episode script.",
      {"notes": "optional reviewer notes to apply on a rewrite"}, long=True)
def generate_script(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import (approve_scene_details, assemble_episode_script,
                           generate_scene_details, read_episode_plan,
                           read_scene_details)
    show, ep = _show(ctx), _ep(ctx)
    plan = read_episode_plan(show, ep)
    if not plan:
        raise RuntimeError("no outline on disk; generate the outline first")
    role_llm = _role_llm(ctx)
    if not (read_scene_details(show, ep) or {}).get("scenes"):
        generate_scene_details(show, ep, llm=role_llm, cfg=ctx.cfg)
    approve_scene_details(show, ep)
    script = assemble_episode_script(show, ep, llm=role_llm, cfg=ctx.cfg,
                                     notes=_card_notes(ctx, args))
    from .. import fsutil
    runs = show.dir / "runs" / f"EP{ep:02d}"
    paths = fsutil.existing_by_mtime(runs.glob("script.r*.json"))
    path = paths[-1] if paths else runs / "script.r1.json"
    ctx.store.attach(ctx.card["id"], _rel(ctx, path), "episode script")
    shots = sum(len(sc.get("shots") or []) for sc in script.get("scenes") or [])
    return {"path": _rel(ctx, path), "scenes": len(script.get("scenes") or []),
            "shots": shots, "summary": script.get("summary", "")}


@tool("revise_script", "Revise the current script from reviewer notes.",
      {"notes": "reviewer notes to apply"}, long=True)
def revise_script(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import revise_script as _revise
    from ..render import _latest_script
    show, ep = _show(ctx), _ep(ctx)
    notes = _card_notes(ctx, args)
    if not notes:
        raise ValueError("notes are required")
    script = _latest_script(show, ep)
    if not script:
        raise RuntimeError("no script to revise")
    revised = _revise(show, ep, script,
                      {"reviewer": {"grade": "Fail", "review": notes}},
                      llm=_role_llm(ctx), cfg=ctx.cfg)
    from .. import fsutil
    runs = show.dir / "runs" / f"EP{ep:02d}"
    paths = fsutil.existing_by_mtime(runs.glob("script.r*.json"))
    path = paths[-1] if paths else runs / "script.r1.json"
    ctx.store.attach(ctx.card["id"], _rel(ctx, path), "revised script")
    return {"path": _rel(ctx, path),
            "scenes": len(revised.get("scenes") or [])}


@tool("run_outline_review", "Run the outline reviewers and return their verdicts.", long=True)
def run_outline_review(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from .. import epstate, review
    from ..planner import read_episode_plan
    show, ep = _show(ctx), _ep(ctx)
    plan = read_episode_plan(show, ep)
    if not plan:
        raise RuntimeError("no outline to review")
    structure = review.run_plan_reviewers(plan, show=show, cfg=ctx.cfg,
                                          llm=ctx.llm, episode=ep,
                                          cancel=ctx.cancel_event)
    room = review.run_plan_room_reviewers(plan, show=show, cfg=ctx.cfg,
                                          llm=ctx.llm, episode=ep,
                                          cancel=ctx.cancel_event)
    merged = {**structure, **room}
    passed = review.plan_passes(merged)
    rounds = int((plan.get("plan_review") or {}).get("rounds") or 0) + 1
    plan["plan_review"] = {
        "rounds": rounds,
        "passed": passed,
        "reviews": {r: {"grade": v.get("grade"), "review": v.get("review", "")}
                    for r, v in merged.items()},
    }
    epstate.save_plan(show, ep, plan)
    failing = {r: v for r, v in merged.items() if not v.get("pass")}
    notes = "; ".join(
        f"{r}: {str(v.get('review') or '').strip()}"
        for r, v in failing.items() if str(v.get("review") or "").strip())
    return {"structure": structure, "room": room, "pass": passed,
            "rounds": rounds, "notes": notes}


@tool("review_scene", "Review one rewritten scene with the blocking outline reviewers.",
      {"scene_id": "scene to review"}, long=True)
def review_scene(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from .. import review
    from ..planner import read_episode_plan
    show, ep = _show(ctx), _ep(ctx)
    payload = ctx.card.get("payload") or {}
    sid = str(args.get("scene_id") or payload.get("scene_id") or "")
    if not sid:
        raise ValueError("no scene_id for review")
    plan = read_episode_plan(show, ep)
    if not plan:
        raise RuntimeError("no outline to review")
    scene = next((s for s in (plan.get("scenes") or [])
                  if isinstance(s, dict) and str(s.get("id")) == sid), None)
    if not scene:
        raise RuntimeError(f"scene {sid} is not in the plan")
    subset = {**plan, "scenes": [scene]}
    scene_dir = show.dir / "runs" / f"EP{ep:02d}" / "reviews" / "scenes" / sid
    structure = review.run_plan_reviewers(
        subset, show=show, cfg=ctx.cfg, llm=ctx.llm, episode=ep,
        notes_dir=scene_dir, step_prefix=f"scene-review-{sid}-structure",
        cancel=ctx.cancel_event)
    room = review.run_plan_room_reviewers(
        subset, show=show, cfg=ctx.cfg, llm=ctx.llm, episode=ep,
        notes_dir=scene_dir, step_prefix=f"scene-review-{sid}",
        notes_prefix=f"scene-{sid}-", cancel=ctx.cancel_event)
    merged = {**structure, **room}
    passed = review.plan_passes(merged)
    failing = {r: v for r, v in merged.items() if not v.get("pass")}
    notes = "; ".join(
        f"{r}: {str(v.get('review') or '').strip()}"
        for r, v in failing.items() if str(v.get("review") or "").strip())
    return {"scene_id": sid, "pass": passed, "structure": structure,
            "room": room, "notes": notes}


@tool("run_review_room", "Run the writers'-room reviewers on the current script.", long=True)
def run_review_room(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from .. import review
    from ..render import _latest_script
    show, ep = _show(ctx), _ep(ctx)
    script = _latest_script(show, ep)
    if not script:
        raise RuntimeError("no script to review")
    results = review.run_reviewers(script, show=show, cfg=ctx.cfg,
                                   llm=ctx.llm, episode=ep)
    return {"pass": review.all_pass(results), "reviews": results,
            "notes": review.review_notes_text(results)}


@tool("prepare_object_prompts",
      "Describe the episode's recurring objects and save their image prompts (LLM only).", long=True)
def prepare_object_prompts(ctx: ToolContext, args: dict[str, Any]) -> Any:
    import re
    from ..planner import read_episode_plan
    from ..storyboard import (_base_object_prompt, _describe_object,
                              _save_object_prompt, _show_style)
    show, ep = _show(ctx), _ep(ctx)
    plan = read_episode_plan(show, ep) or {}
    objs = [str(o) for o in (plan.get("objects") or []) if o][:6]
    llm = _role_llm(ctx)
    for name in objs:
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
        if not slug:
            continue
        desc = _describe_object(show, ep, name, cfg=ctx.cfg, llm=llm)
        prompt = _base_object_prompt(name, _show_style(show), desc,
                                     show.ref_medium())
        _save_object_prompt(show, ep, slug, prompt)
    return {"objects": objs}


@tool("build_keyframes",
      "Generate object refs + shot keyframes with ComfyUI (no LLM).")
def build_keyframes(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..planner import read_episode_plan
    from ..storyboard import generate_object_refs, generate_shot_keyframes
    show, ep = _show(ctx), _ep(ctx)
    plan = read_episode_plan(show, ep) or {}
    objs = [str(o) for o in (plan.get("objects") or []) if o][:6]
    from ..gpu_manager import get_gpu_manager
    gm = get_gpu_manager(ctx.cfg)
    try:
        objects = generate_object_refs(show, ep, cfg=ctx.cfg, objs=objs)
        previews = generate_shot_keyframes(show, ep, cfg=ctx.cfg)
    finally:
        gm.unload_comfy()
    ctx.store.attach(ctx.card["id"],
                     _rel(ctx, show.dir / "runs" / f"EP{ep:02d}" / "storyboard"),
                     "storyboard previews")
    return {"objects": objects, "previews": previews}


@tool("generate_storyboard", "Generate keyframe previews and object refs for the script.")
def generate_storyboard(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..storyboard import generate_object_refs, generate_shot_keyframes
    show, ep = _show(ctx), _ep(ctx)
    objects = generate_object_refs(show, ep, cfg=ctx.cfg)
    previews = generate_shot_keyframes(show, ep, cfg=ctx.cfg)
    ctx.store.attach(ctx.card["id"], _rel(ctx, show.dir / "runs" / f"EP{ep:02d}" / "storyboard"),
                     "storyboard previews")
    return {"object_refs": objects, "previews": previews}


# ---------------------------------------------------------------------------
# render tools
# ---------------------------------------------------------------------------

@tool("render_shot", "Render one shot with H3. Defaults to this card's shot.",
      {"shot_id": "optional shot id"})
def render_shot(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..approval import shot_approvals
    from ..gpu_manager import get_gpu_manager
    from ..render import _latest_script, _render_client, _render_shot
    show, ep = _show(ctx), _ep(ctx)
    sid = str(args.get("shot_id") or (ctx.card.get("payload") or {}).get("shot_id") or "")
    if not sid:
        raise ValueError("shot_id is required")
    script, scene, si, hi = _shot(show, ep, sid)
    retake = int((shot_approvals(show, ep).get(sid, {}) or {}).get("retake_count", 0) or 0)
    client = _render_client(ctx.cfg)
    gm = get_gpu_manager(ctx.cfg)
    gm.unload_comfy()
    gm.unload_llm()
    if not gm.ensure_h3():
        raise RuntimeError("H3 instance is not available on :8188")
    try:
        out = _render_shot(show, ep, client, ctx.cfg, script, scene,
                           next(s for s in scene["shots"] if s.get("id") == sid),
                           seed=si * 100 + hi + 1000 * retake)
    finally:
        gm.unload_h3()
    ctx.store.attach(ctx.card["id"], _rel(ctx, out), f"{sid} render")
    return {"shot_id": sid, "path": _rel(ctx, out), "retake": retake}


@tool("shot_qc", "Run the STT dialogue check on a rendered shot.",
      {"shot_id": "optional shot id"})
def shot_qc(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..qc import run_shot_qc
    show, ep = _show(ctx), _ep(ctx)
    sid = str(args.get("shot_id") or (ctx.card.get("payload") or {}).get("shot_id") or "")
    if not sid:
        raise ValueError("shot_id is required")
    _, scene, _, _ = _shot(show, ep, sid)
    shot = next(s for s in scene["shots"] if s.get("id") == sid)
    report = run_shot_qc(show, ep, _clip(show, ep, sid), shot, cfg=ctx.cfg)
    path = show.dir / "runs" / f"EP{ep:02d}" / "qc" / "summary.json"
    if path.exists():
        ctx.store.attach(ctx.card["id"], _rel(ctx, path), f"{sid} STT QC")
    return report


@tool("shot_vcheck", "Run the visual (VLM) check on a rendered shot.",
      {"shot_id": "optional shot id"})
def shot_vcheck(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..vcheck import run_shot_vcheck
    show, ep = _show(ctx), _ep(ctx)
    sid = str(args.get("shot_id") or (ctx.card.get("payload") or {}).get("shot_id") or "")
    if not sid:
        raise ValueError("shot_id is required")
    _, scene, _, _ = _shot(show, ep, sid)
    shot = next(s for s in scene["shots"] if s.get("id") == sid)
    from ..gpu_manager import ServiceType, get_gpu_manager
    vck = ctx.cfg.get("qc", "vcheck", {}) or {}
    service = ServiceType.VLLM if vck.get("server") == "vllm" else ServiceType.VLM
    with get_gpu_manager(ctx.cfg).acquire(service):
        report = run_shot_vcheck(show, ep, _clip(show, ep, sid), shot, cfg=ctx.cfg)
    path = show.dir / "runs" / f"EP{ep:02d}" / "vcheck" / "summary.json"
    if path.exists():
        ctx.store.attach(ctx.card["id"], _rel(ctx, path), f"{sid} visual QC")
    return report


@tool("revise_shot_prompt", "Revise a shot's prompt from rejection notes and queue a retake.",
      {"shot_id": "optional shot id", "notes": "what is wrong"})
def revise_shot_prompt(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..approval import reject_shot
    show, ep = _show(ctx), _ep(ctx)
    sid = str(args.get("shot_id") or (ctx.card.get("payload") or {}).get("shot_id") or "")
    notes = str(args.get("notes") or "").strip()
    if not sid or not notes:
        raise ValueError("shot_id and notes are required")
    return {"messages": reject_shot(show, ep, sid, notes=notes, llm=_role_llm(ctx))}


@tool("run_episode_qc", "Run STT + VLM checks on every rendered shot.")
def run_episode_qc(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..gpu_manager import ServiceType, get_gpu_manager
    from ..qc import run_shot_qc
    from ..render import _latest_script
    from ..vcheck import run_shot_vcheck
    show, ep = _show(ctx), _ep(ctx)
    script = _latest_script(show, ep) or {}
    shots = [(shot, _clip(show, ep, shot.get("id")))
             for scene in script.get("scenes") or []
             for shot in scene.get("shots") or []]
    reports: dict[str, dict[str, Any]] = {}
    checked = 0
    # STT is CPU-only: run it all first, then hold the VLM for the visual pass.
    for shot, clip in shots:
        sid = shot.get("id")
        if not clip.exists():
            reports[sid] = {"qc": {"verdict": "fail", "issues": ["clip missing"]}}
            continue
        checked += 1
        reports[sid] = {"qc": run_shot_qc(show, ep, clip, shot, cfg=ctx.cfg)}
    vck = ctx.cfg.get("qc", "vcheck", {}) or {}
    service = ServiceType.VLLM if vck.get("server") == "vllm" else ServiceType.VLM
    with get_gpu_manager(ctx.cfg).acquire(service):
        for shot, clip in shots:
            if not clip.exists():
                continue
            sid = shot.get("id")
            reports[sid]["vcheck"] = run_shot_vcheck(
                show, ep, clip, shot, cfg=ctx.cfg)
    failures = []
    for sid, report in reports.items():
        qc = report.get("qc") or {}
        vc = report.get("vcheck") or {}
        if qc.get("verdict") == "fail" or vc.get("verdict") == "fail":
            failures.append({"shot_id": sid, "qc": qc, "vcheck": vc})
    for name in ("qc", "vcheck"):
        path = show.dir / "runs" / f"EP{ep:02d}" / name / "summary.json"
        if path.exists():
            ctx.store.attach(ctx.card["id"], _rel(ctx, path), f"episode {name} report")
    return {"checked": checked, "failures": failures}


@tool("apply_qc_fixes", "Revise failed shot prompts and requeue their renders.", long=True)
def apply_qc_fixes(ctx: ToolContext, args: dict[str, Any]) -> Any:
    import time
    from ..approval import reject_shot
    from .cards import new_card
    show, ep = _show(ctx), _ep(ctx)
    payload = ctx.card.get("payload") or {}
    failures = payload.get("failures") or []
    if not failures:
        raise ValueError("no QC failures on this card")
    milestone = str(payload.get("render_milestone")
                    or f"{show.show_id}.ep{ep:02d}.render")
    llm = _role_llm(ctx)
    made: list[str] = []
    for failure in failures:
        sid = str(failure.get("shot_id") or "")
        if not sid:
            continue
        issues: list[str] = []
        for key in ("qc", "vcheck"):
            report = failure.get(key) or {}
            issues.extend(str(i) for i in (report.get("issues") or []))
            fix = str(report.get("prompt_fix") or "").strip()
            if fix:
                issues.append(fix)
        notes = "; ".join(issues) or "QC failed"
        reject_shot(show, ep, sid, notes=notes, llm=llm)
        cid = f"{show.show_id}.ep{ep:02d}.shot.{sid}.r{int(time.time() * 1000)}"
        ctx.store.upsert(new_card(
            cid, kind="render_shot", title=f"Re-render EP{ep:02d} shot {sid}",
            show_id=show.show_id, episode=ep, role="render_exec",
            lane="video", priority=4, max_attempts=3,
            payload={"tool": "render_shot", "args": {"shot_id": sid},
                     "shot_id": sid, "parent": milestone},
        ))
        made.append(cid)
    qc_id = str(payload.get("qc_card") or "")
    qc = ctx.store.get(qc_id) if qc_id else None
    attempts = 0
    if qc:
        attempts = int(qc.get("attempts") or 0) + 1
        cap = int(qc.get("max_attempts") or 4)
        ctx.store.update(qc_id, depends_on=made, attempts=attempts)
        if qc.get("state") == "blocked" and attempts < cap:
            ctx.store.advance(qc_id, "planned", "fix", "QC retry")
        elif attempts >= cap:
            ctx.store.note(qc_id, "fixer", f"QC retake cap reached ({attempts})")
    return {"render_cards": made, "revised": len(made),
            "attempt": attempts if qc else 0}


@tool("assemble_episode", "Concat accepted shot clips into the episode MP4.")
def assemble_episode(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from ..clients.ffmpeg import concat_clips, normalize_loudness
    from ..render import _latest_script
    show, ep = _show(ctx), _ep(ctx)
    script = _latest_script(show, ep) or {}
    clips = []
    for scene in script.get("scenes") or []:
        for shot in scene.get("shots") or []:
            clip = _clip(show, ep, shot.get("id"))
            if clip.exists():
                clips.append(clip)
    if not clips:
        raise RuntimeError("no rendered clips to assemble")
    out = show.dir / "runs" / f"EP{ep:02d}" / "episode.mp4"
    concat_clips(clips, out)
    normalized = normalize_loudness(out, out.with_name("episode_norm.mp4"))
    final = normalized or out
    ctx.store.attach(ctx.card["id"], _rel(ctx, final), "episode assembly")
    return {"path": _rel(ctx, final), "clips": len(clips)}


@tool("episode_status", "Report which episode artifacts exist.")
def episode_status(ctx: ToolContext, args: dict[str, Any]) -> Any:
    show, ep = _show(ctx), _ep(ctx)
    runs = show.dir / "runs" / f"EP{ep:02d}"
    video = runs / "video"
    return {
        "plan": (runs / "plan.json").exists(),
        "script": bool(list(runs.glob("script.r*.json"))),
        "storyboard": sorted(p.name for p in (runs / "storyboard").glob("*.mp4"))
        if (runs / "storyboard").exists() else [],
        "video": sorted(p.name for p in video.glob("*.mp4")) if video.exists() else [],
        "episode": (runs / "episode.mp4").exists(),
        "at": now(),
    }
