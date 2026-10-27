"""Apply agent-written Gate 0 artifacts (concept, bible, cast, locations).

Deterministic only: validates the files, writes normalized show artifacts, sets
the bootstrap state for human review, and attaches the artifacts to the card.
No LLM calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.board.store import BoardSet
from studio.config import get_config
from studio.show import Show


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path.name}: must be a mapping")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    args = ap.parse_args()

    cfg = get_config()
    store = BoardSet(cfg)
    card = store.get(args.card)
    if not card:
        raise SystemExit(f"no card {args.card!r}")
    show = Show(str(card.get("show_id") or ""))
    payload = card.get("payload") or {}
    step = str(payload.get("step") or "").strip()
    target_char = str(payload.get("char") or "").strip()
    profile = show.style_profile(cfg)
    maturity = str(profile.get("maturity") or "").strip()

    concept_path = show.dir / "concept.json"
    if not concept_path.exists():
        raise SystemExit("missing concept.json")
    concept = json.loads(concept_path.read_text(encoding="utf-8-sig"))
    if not isinstance(concept, dict) or not concept.get("title") or not concept.get("logline"):
        raise SystemExit("concept.json needs title and logline")
    if maturity:
        concept["maturity"] = maturity
    show.write_concept(concept)

    bible_path = show.dir / "bible.yaml"
    if not bible_path.exists():
        raise SystemExit("missing bible.yaml")
    bible = _read_yaml(bible_path)
    for key in ("title", "logline", "genre", "tone", "cast"):
        if not bible.get(key):
            raise SystemExit(f"bible.yaml missing {key}")
    if maturity:
        bible["content_policy"] = maturity
    bible.setdefault("art_direction", {})
    bible.setdefault("runtime_target_s", profile.get(
        "runtime_target_s", 1320))
    show.write_bible(bible)

    cast = []
    for entry in bible.get("cast") or []:
        if isinstance(entry, dict) and entry.get("name"):
            cast.append(str(entry["name"]).strip())
    if not cast:
        raise SystemExit("bible.yaml cast is empty")

    char_files = []
    for p in sorted(show.characters_dir.glob("*.yaml")):
        data = _read_yaml(p)
        if data.get("name"):
            char_files.append(data)
    by_name = {str(c.get("name")).lower(): c for c in char_files}
    characters = []
    for index, name in enumerate(cast, start=1):
        char = by_name.get(name.lower())
        if not char:
            raise SystemExit(f"missing character sheet for {name!r}")
        char["name"] = name
        char["id"] = char.get("id") or re.sub(
            r"[^a-z0-9-]+", "-", name.lower()).strip("-")
        for key in ("appearance_canonical", "identity_canonical",
                    "wardrobe_canonical", "voice_sample_text"):
            if not char.get(key):
                raise SystemExit(f"{name}: missing {key}")
        if len(str(char["voice_sample_text"]).split()) < 50:
            raise SystemExit(f"{name}: voice_sample_text is too short")
        voice = char.get("voice") or {}
        if not isinstance(voice, dict) or not voice.get("voice_description"):
            raise SystemExit(f"{name}: missing voice.voice_description")
        char.setdefault("is_person", True)
        char.setdefault("gender", "none")
        char.setdefault("appearance_notes", "")
        char.setdefault("personality", [])
        char.setdefault("traits_for_llm", str(char.get("role") or ""))
        char.setdefault("h3_slot", f"@char{index}")
        char["voice"] = {"mode": "designed",
                         "voice_description": voice["voice_description"]}
        show.write_character(char)
        characters.append(char)

    scene_files = []
    for p in sorted(show.scenes_dir.glob("*.yaml")):
        data = _read_yaml(p)
        if data.get("name") or data.get("id"):
            scene_files.append(data)
    if len(scene_files) < 3:
        raise SystemExit("need at least 3 location YAMLs")
    for scene in scene_files:
        scene.setdefault("id", re.sub(
            r"[^a-z0-9-]+", "-", str(scene.get("name") or "").lower()).strip("-"))
        for key in ("name", "description", "setting_prompt"):
            if not scene.get(key):
                raise SystemExit(f"scene {scene.get('id')}: missing {key}")
        show.write_scene(scene)

    st = show.bootstrap_state()
    st["complete"] = False
    prior = {str(c.get("name") or "").lower(): dict(c)
             for c in (st.get("characters") or [])}
    if step in ("", "concept"):
        st.setdefault("concept", {})["status"] = "pending"
        st.setdefault("bible", {})["status"] = "pending"
        st.setdefault("scenes", {})["status"] = "pending"
        for c in characters:
            c["proposal"] = "pending"
            c["refs"] = ""
            c["voice"] = ""
    elif step == "bible":
        st.setdefault("bible", {})["status"] = "pending"
        st.setdefault("scenes", {})["status"] = "pending"
        for c in characters:
            c["proposal"] = "pending"
            c["refs"] = ""
            c["voice"] = ""
    elif step == "character":
        if not target_char:
            raise SystemExit("character revision needs payload.char")
        for c in characters:
            old = prior.get(c["name"].lower(), {})
            if c["name"].lower() == target_char.lower():
                c["proposal"] = "pending"
                c["refs"] = ""
                c["voice"] = ""
            else:
                c["proposal"] = old.get("proposal", "pending")
                c["refs"] = old.get("refs", "")
                c["voice"] = old.get("voice", "")
    elif step == "scenes":
        st.setdefault("scenes", {})["status"] = "pending"
        for c in characters:
            old = prior.get(c["name"].lower(), {})
            c["proposal"] = old.get("proposal", "pending")
            c["refs"] = old.get("refs", "")
            c["voice"] = old.get("voice", "")
    else:
        raise SystemExit(f"unknown bootstrap step {step!r}")

    st["characters"] = [
        {"name": c["name"], "proposal": c.get("proposal", "pending"),
         "refs": c.get("refs", ""), "voice": c.get("voice", "")}
        for c in characters
    ]
    show.set_bootstrap_state(st)

    paths = [concept_path, bible_path]
    paths += [show.characters_dir / f"{c['id']}.yaml" for c in characters]
    paths += [show.scenes_dir / f"{s['id']}.yaml" for s in scene_files]
    for path in paths:
        if path.exists():
            rel = path.relative_to(Path(cfg.root)).as_posix()
            store.attach(card["id"], rel, path.stem)

    for child in store.all(show_id=show.show_id):
        if (child.get("kind") in ("gate0_images", "gate0_voices")
                and child.get("state") not in ("accepted", "blocked")):
            store.update(child["id"], depends_on=[card["id"]])

    store.update(card["id"], result={
        "step": step or "create",
        "concept": concept.get("title"),
        "characters": [c["name"] for c in characters],
        "scenes": [s["id"] for s in scene_files],
    })
    store.close()
    print(f"bootstrapped {concept.get('title')}: {len(characters)} cast, "
          f"{len(scene_files)} locations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
