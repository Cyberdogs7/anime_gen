"""Command-line interface. See DESIGN.md §0 (agent board) / §20.1.

Commands: board | request-run | init-show | bootstrap | status
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

import yaml

from . import fsutil
from .config import DEFAULT_RUNTIME_TARGET_S, Config, get_config
from .db import DB
from .setup import run_setup
from .verify import run_verify

log = logging.getLogger(__name__)


def _log_setup(verbose: bool = False) -> None:
    # lms/comfy subprocess output carries unicode spinner glyphs; console-safe printing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def _cmd_verify(args: argparse.Namespace) -> int:
    return run_verify()


def _cmd_setup(args: argparse.Namespace) -> int:
    if args.lmstudio:
        from .remote import ServiceOps, resolve_client
        if args.remote:
            client = resolve_client(get_config(), args.remote)
            print(f"[setup] provisioning LM Studio on remote '{args.remote}'...")
            print(client.provision_lmstudio())
        else:
            print("[setup] provisioning LM Studio locally...")
            print(ServiceOps().provision_lmstudio())
        return 0
    return run_setup(create_venv=args.venv, install_deps=args.install)


# ---------------------------------------------------------------------------
# lms / comfy remote control
# ---------------------------------------------------------------------------

def _cmd_lms(args: argparse.Namespace) -> int:
    from .remote import ServiceOps, resolve_client
    op = args.op
    if args.remote:
        client = resolve_client(get_config(), args.remote)
        fn = {"start": client.lms_start, "unload": client.lms_unload,
              "status": client.lms_status}.get(op)
        if op == "load":
            fn = lambda: client.lms_load(args.model, args.gpu_ratio)  # noqa: E731
        elif op == "get":
            fn = lambda: client.lms_get(args.model)  # noqa: E731
        result = fn()
    else:
        ops = ServiceOps()
        fn = {"start": ops.lms_start, "unload": ops.lms_unload,
              "status": ops.lms_status}.get(op)
        if op == "load":
            fn = lambda: ops.lms_load(args.model, args.gpu_ratio)  # noqa: E731
        elif op == "get":
            fn = lambda: ops.lms_get(args.model)  # noqa: E731
        result = fn()
    print(result.get("ok", False) and "[ OK ]" or "[FAIL]", result.get("detail", result))
    return 0 if result.get("ok") else 1


def _cmd_comfy(args: argparse.Namespace) -> int:
    from .remote import ServiceOps, resolve_client
    if args.remote:
        client = resolve_client(get_config(), args.remote)
        result = (client.comfy_start(args.which) if args.op == "start"
                  else client.comfy_stop(args.which))
    else:
        ops = ServiceOps()
        result = (ops.comfy_start(args.which) if args.op == "start"
                  else ops.comfy_stop(args.which))
    print(result.get("ok", False) and "[ OK ]" or "[FAIL]", result.get("detail", result))
    return 0 if result.get("ok") else 1


def _cmd_remote_agent(args: argparse.Namespace) -> int:
    from .remote import run_agent
    run_agent(port=args.port, bind=args.bind, token=args.token)
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .remote import resolve_client
    client = resolve_client(get_config(), args.remote)
    print(f"[doctor] verifying remote '{args.remote}'...")
    result = client.verify()
    print(result.get("detail", ""))
    return 0 if result.get("ok") else 1


def _cmd_script(args: argparse.Namespace) -> int:
    """Generate an episode script via the single pipeline path (prose -> review
    -> shots). Requires the show's bible/cast;
    uses the approved plan as the skeleton when one exists."""
    from .clients.lmstudio import LMStudioClient
    from .planner import assemble_episode_script
    from .show import Show
    show = Show(args.show)
    llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=900)
    script = assemble_episode_script(show, args.episode, llm=llm)
    review = script.get("_review", {})
    print(f"episode {args.episode}: rounds={review.get('rounds', 1)} "
          f"passed={review.get('passed')}")
    for n in review.get("notes", []) or []:
        print(f"  [{n.get('severity', '?')}] ({n.get('reviewer', '?')}) {n.get('note', '')}")
    scripts = sorted((show.dir / "runs" / f"EP{args.episode:02d}").glob("script.r*.json"))
    print("script:", scripts[-1] if scripts else "(missing)")
    return 0 if review.get("passed") else 1


def _cmd_prose(args: argparse.Namespace) -> int:
    """Prose-first episode: whole-episode screenplay -> story review -> shots by need."""
    from .clients.lmstudio import LMStudioClient
    from .prose import (breakdown_prose_to_shots, generate_episode_prose,
                        write_prose_review_loop)
    from .review import all_pass
    from .show import Show
    show = Show(args.show)
    llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=900)
    prose = generate_episode_prose(show, args.episode, llm=llm)
    print(f"prose draft: {len(prose)} chars")
    prose, reviews = write_prose_review_loop(show, args.episode, prose, llm=llm,
                                             max_revisions=args.max_revisions)
    for name, r in reviews.items():
        print(f"  [{'PASS' if r['pass'] else 'FAIL'}] {name:12s} "
              f"notes={len(r.get('notes', []))}")
    print("PROSE ALL PASS" if all_pass(reviews) else "PROSE REVISION REQUIRED")
    if args.breakdown and all_pass(reviews):
        result = breakdown_prose_to_shots(show, args.episode, llm=llm)
        print(f"breakdown: {len(result['scenes'])} scenes -> "
              f"{sum(len(s.get('shots', [])) for s in result['scenes'])} shots")
    return 0 if all_pass(reviews) else 1


def _cmd_dump(args: argparse.Namespace) -> int:
    """Print EVERYTHING for an episode (full text, nothing truncated) for debugging."""
    from .show import Show
    show = Show(args.show)
    d = show.dir / "runs" / f"EP{args.episode:02d}"
    if not d.exists():
        print(f"no runs/EP{args.episode:02d} for {args.show}")
        return 1
    def _show_file(name: str) -> None:
        p = d / name
        print(f"\n===== {name} {'(missing)' if not p.exists() else f'({p.stat().st_size} bytes)'} =====")
        if p.exists():
            try:
                print(p.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"<unreadable: {exc}>")
    for name in ("episode_prose.txt", "dialogue.txt", "plan.json",
                 "scene_details.json", "script_reject.json"):
        _show_file(name)
    scripts = sorted(d.glob("script.r*.json"))
    print(f"\n===== scripts: {[s.name for s in scripts] or '(none)'} =====")
    for s in scripts:
        try:
            script = json.loads(s.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"{s.name}: <unparseable: {exc}>")
            continue
        print(f"\n--- {s.name}: {len(script.get('scenes', []))} scenes, "
              f"review={json.dumps(script.get('_review', {}), ensure_ascii=False)} ---")
        for sc in script.get("scenes", []):
            print(f"\n## {sc.get('id')} {sc.get('location', '')}: {sc.get('summary', '')}")
            for sh in sc.get("shots", []) or []:
                print(f"  [{sh.get('id')}] ({sh.get('type', '')}/{sh.get('duration_s', '')}s) "
                      f"{sh.get('action', '')}")
                for dlg in sh.get("dialogue", []) or []:
                    if isinstance(dlg, dict):
                        print(f"    {dlg.get('char')}: {dlg.get('line')}")
    rdir = d / "reviews"
    if rdir.exists():
        print("\n===== reviews (FULL notes, every severity) =====")
        for f in sorted(rdir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"{f.name}: <unparseable: {exc}>")
                continue
            print(f"\n--- {f.name}: pass={data.get('pass')} score={data.get('score')} ---")
            for n in data.get("notes", []) or []:
                if isinstance(n, dict):
                    print(f"  [{n.get('severity', '?')}] {n.get('scene', '')} {n.get('note', '')}")
                else:
                    print(f"  {n}")
    return 0


def _cmd_h3status(args: argparse.Namespace) -> int:
    """Show H3 model download progress (local node A = this machine)."""
    from .h3download import download_status
    lines, done, pct = download_status()
    for line in lines:
        print(line)
    if done:
        print("ALL H3 MODELS PRESENT - ready to launch and tune H3.")
        return 0
    print(f"overall: {pct:.1f}%  |  rerun `studio.py h3status` to refresh.")
    return 1


def _cmd_shot(args: argparse.Namespace) -> int:
    from .clients.comfy import ComfyClient
    from .h3 import build_h3_shot_workflow, run_h3_shot
    cfg = get_config()
    h3 = cfg.get("comfy", "h3", {})
    use_spectrum = h3.get("spectrum", True) if args.spectrum is None else args.spectrum
    use_fbc = h3.get("first_block_cache", False) if args.fbc is None else args.fbc
    sampler = h3.get("sampler") or ("res_multistep" if use_spectrum else "sa_solver")
    steps = args.steps or int(h3.get("steps", 8))
    node_cfg = cfg.get("comfy", "nodes", {}).get("renderer", {})
    client = ComfyClient(node_cfg.get("url", "http://127.0.0.1:8188"))
    ref_filenames = []
    for path in args.ref_image:
        fname = client.upload_image(path)
        print(f"[ ref ] uploaded {path} -> {fname}")
        ref_filenames.append(fname)
    wf = build_h3_shot_workflow(args.prompt, args.duration, args.seed, cfg=cfg,
                                segment_prompt=args.segment, steps=steps,
                                sampler_name=sampler,
                                use_spectrum=use_spectrum, use_first_block_cache=use_fbc,
                                ref_images=ref_filenames or None)
    out = Path(args.out) if args.out else cfg.root / "cache" / "h3_shot.mp4"
    print(f"rendering {args.duration}s H3 shot (seed={args.seed}, steps={steps}, "
          f"sampler={sampler}, spectrum={use_spectrum}, fbc={use_fbc}) - be patient...")
    try:
        final = run_h3_shot(client, wf, out, timeout_s=args.timeout)
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[ OK ] shot saved: {final}")
    return 0


def _cmd_extend(args: argparse.Namespace) -> int:
    from .clients.comfy import ComfyClient
    from .clients.ffmpeg import duration_s
    from .h3 import build_h3_retake_workflow, run_h3_shot
    from pathlib import Path
    cfg = get_config()
    h3 = cfg.get("comfy", "h3", {})
    use_spectrum = h3.get("spectrum", True) if args.spectrum is None else args.spectrum
    use_fbc = h3.get("first_block_cache", False) if args.fbc is None else args.fbc
    sampler = h3.get("sampler") or ("res_multistep" if use_spectrum else "sa_solver")
    steps = args.steps or int(h3.get("steps", 8))
    node_cfg = cfg.get("comfy", "nodes", {}).get("renderer", {})
    client = ComfyClient(node_cfg.get("url", "http://127.0.0.1:8188"))
    dur = duration_s(args.video)
    if not dur:
        print(f"[FAIL] could not probe {args.video} (ffprobe unavailable?)")
        return 1
    base_frames = int(round(dur * 24))
    retake_length = max(24, int(round(args.duration * 24)))
    if args.start == "end":
        retake_start = base_frames
    else:
        retake_start = int(args.start)
    fname = client.upload_video(args.video)
    print(f"[ base ] {args.video} -> {fname} ({base_frames} frames @24fps)")
    print(f"[ range ] start={retake_start} length={retake_length} "
          f"({'extension past end' if retake_start + retake_length > base_frames else 'in-place retake'})")
    wf = build_h3_retake_workflow(args.prompt, fname, base_frames, retake_start, retake_length,
                                   seed=args.seed, steps=steps, sampler_name=sampler,
                                   use_spectrum=use_spectrum, use_first_block_cache=use_fbc)
    out = Path(args.out) if args.out else cfg.root / "cache" / "h3_extend.mp4"
    print(f"rendering {args.duration}s extension (seed={args.seed}, steps={steps}, "
          f"sampler={sampler}, spectrum={use_spectrum}, fbc={use_fbc}) - be patient...")
    try:
        final = run_h3_shot(client, wf, out, timeout_s=args.timeout)
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1
    try:
        from .clients.ffmpeg import normalize_spliced_loudness
        splice_sec = retake_start / 24.0
        norm = normalize_spliced_loudness(final, final.with_name(final.stem + "_norm.mp4"),
                                           [splice_sec])
        if norm:
            final = norm
            print("[ norm ] per-segment EBU R128 loudness normalization applied")
    except Exception as exc:
        print(f"[ warn ] loudness normalization skipped: {exc}")
    print(f"[ OK ] extended render saved: {final}")
    return 0


def _cmd_scene_master(args: argparse.Namespace) -> int:
    from .render_scene import render_scene_master
    from .clients.comfy import ComfyClient
    from .show import Show
    import json as _json
    cfg = get_config()
    show = Show(args.show)
    d = show.dir / "runs" / f"EP{args.episode:02d}"
    scripts = fsutil.existing_by_mtime(d.glob("script.r*.json"))
    if not scripts:
        print(f"[FAIL] no script for {args.show} EP{args.episode:02d}")
        return 1
    script = _json.loads(scripts[-1].read_text(encoding="utf-8"))
    scene = next((s for s in script.get("scenes", []) if s.get("id") == args.scene), None)
    if not scene:
        print(f"[FAIL] scene {args.scene} not found")
        return 1
    node_cfg = cfg.get("comfy", "nodes", {}).get("renderer", {})
    client = ComfyClient(node_cfg.get("url", "http://127.0.0.1:8188"))
    ms = cfg.get("pipeline", "multishot", {}) or {}
    print(f"[scene] {args.scene} {len(scene.get('shots', []))} shots -> seamless master (multishot {ms.get('continuity', 'context_pin')})")
    try:
        out = render_scene_master(show, args.episode, scene, client, cfg=cfg, seed=args.seed)
        print(f"[ OK ] master saved: {out}")
        return 0
    except Exception as exc:
        print(f"[FAIL] {exc}")
        import traceback; traceback.print_exc()
        return 1


def _cmd_review(args: argparse.Namespace) -> int:
    from .clients.lmstudio import LMStudioClient
    from .review import all_pass, run_reviewers
    from .show import Show
    script = json.loads(Path(args.script).read_text(encoding="utf-8-sig"))
    show = Show(args.show) if args.show else None
    llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=600)
    results = run_reviewers(script, show=show, llm=llm, episode=args.episode, round_no=args.round)
    for name, r in results.items():
        n = len(r.get("notes", []) or [])
        print(f"{'[ PASS ]' if r['pass'] else '[ FAIL ]'} {name:12s} score={r.get('score')} notes={n}")
        # Show ALL HIGHs first, then the rest — truncating hid the blocking set.
        def _rank(note):
            sev = (note.get("severity", "") if isinstance(note, dict) else "").upper()
            return {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(sev, 3)
        ordered = sorted((r.get("notes", []) or []),
                         key=lambda x: _rank(x) if isinstance(x, dict) else 3)
        for note in ordered:
            text = note.get("note") if isinstance(note, dict) else str(note)
            sev = note.get("severity", "") if isinstance(note, dict) else ""
            print(f"    - [{sev or '?'}] {text}")
    print("ALL PASS" if all_pass(results) else "REVISION REQUIRED")
    return 0 if all_pass(results) else 1


def _cmd_rewrite(args: argparse.Namespace) -> int:
    from .clients.lmstudio import LMStudioClient
    from .rewrite import regenerate_outline_once, rewrite_script_once
    from .show import Show
    cfg = get_config()
    show = Show(args.show)
    llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=600)
    if args.stage == "outline":
        plan = regenerate_outline_once(show, args.episode, args.model,
                                       cfg=cfg, llm=llm, notes=args.notes)
        pr = plan.get("plan_review") or {}
        print(f"{args.show} EP{args.episode:02d}: regenerated outline with {args.model}")
        print(f"  passed={pr.get('passed')}  rounds={pr.get('rounds')}  "
              f"notes={len(pr.get('notes') or [])}")
        return 0
    script = rewrite_script_once(show, args.episode, args.model,
                                 cfg=cfg, llm=llm, notes=args.notes)
    rev = script.get("_review") or {}
    print(f"{args.show} EP{args.episode:02d}: rewrote script with {args.model}")
    print(f"  passed={rev.get('passed')}  rounds={rev.get('rounds')}  "
          f"notes={len(rev.get('notes') or [])}")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from .evals import run_eval
    run_eval(args.show, args.episode, args.stage, args.runs,
             temperature=args.temperature, quality=not args.no_quality,
             out_dir=args.out, label=args.label)
    return 0


def _cmd_eval_compare(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path as _Path
    from .evals import compare_reports, print_comparison
    a = json.loads(_Path(args.a).read_text(encoding="utf-8"))
    b = json.loads(_Path(args.b).read_text(encoding="utf-8"))
    print_comparison(compare_reports(a, b, args.a, args.b))
    return 0


def _cmd_learn(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path
    from .learn import aggregate_reviews, distill_rules, promote_rules, summarize
    show_dir = _Path(get_config().root) / "shows" / args.show
    notes = aggregate_reviews(show_dir, args.episode, round_no=args.round)
    if not notes:
        print(f"no review notes for {args.show} EP{args.episode:02d}")
        return 1
    s = summarize(notes)
    print(f"{args.show} EP{args.episode:02d}: {s['total']} notes ({s['high']} HIGH)")
    for reviewer, counts in sorted(s["by_reviewer"].items()):
        print(f"  {reviewer:12s} HIGH={counts.get('HIGH',0)} MED={counts.get('MEDIUM',0)} "
              f"LOW={counts.get('LOW',0)} total={counts.get('total',0)}")
    rules = distill_rules(notes, high_only=not args.all)
    for r in rules:
        print(f"  - {r}")
    if args.promote:
        promoted = promote_rules(show_dir, rules)
        print(f"director_notes.json now holds {len(promoted)} constraints.")
    elif rules:
        print("Re-run with --promote to append these to director_notes.json (deduped).")
    return 0


def _cmd_keyframe(args: argparse.Namespace) -> int:
    from .remote import ServiceOps, resolve_client
    if args.remote:
        client = resolve_client(get_config(), args.remote)
        result = client.generate_image(args.prompt, args.seed, args.aspect, args.out, args.lora)
    else:
        result = ServiceOps().generate_image(args.prompt, args.seed, args.aspect, args.out,
                                             args.lora)
    print(result.get("ok", False) and "[ OK ]" or "[FAIL]", result.get("detail", result))
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# init-show
# ---------------------------------------------------------------------------

def _cmd_init_show(args: argparse.Namespace) -> int:
    cfg = get_config()
    show_id = (args.name or "").strip().lower().replace(" ", "-")
    if not show_id:
        print("error: --name is required")
        return 2
    show_dir = cfg.show_path(show_id)
    if (show_dir / "bible.yaml").exists():
        print(f"show '{show_id}' already exists at {show_dir}")
        return 1
    for sub in ("characters", "voices", "scenes"):
        (show_dir / sub).mkdir(parents=True, exist_ok=True)
    (show_dir / "continuity").mkdir(parents=True, exist_ok=True)

    bible = {
        "series": {
            "title": args.name,
            "genre": cfg.get("show_profile", "genre", []),
            "tone": cfg.get("show_profile", "tone", []),
            "language": "en",
            "runtime_target_s": cfg.get("show_profile", "runtime_target_s", DEFAULT_RUNTIME_TARGET_S),
            "style_guide": "",  # rendering/linework only; filled by the Gate 0 bootstrap
            "art_direction": {},  # camera/lighting/palette reference; never in prompts
            # Per-show style references (set these in the show's bible).
            "baseline": "",
            "fanservice_baseline": "",
            # Optional per-show art medium for reference images; empty = the show
            # declares no medium (the code imposes none).
            "medium": "",
        },
        "arcs": [],
        "overall_plotline": None,
        "content_policy": cfg.get("show_profile", "maturity") or "",
    }
    (show_dir / "bible.yaml").write_text(
        yaml.safe_dump(bible, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    db = DB(cfg.db_path)
    db.set_continuity(show_id, {"episode": 0, "world": {}, "characters": {},
                                "plotlines": [], "unresolved_threads": [],
                                "continuity_rules": []})
    db.close()
    print(f"created show '{show_id}' at {show_dir}")

    from .board.gate0 import bootstrap_cards
    from .board.store import BoardSet
    store = BoardSet(cfg)
    cards = [store.upsert(c) for c in bootstrap_cards(show_id, brief=args.brief,
                                                      auto=bool(args.generate))]
    store.close()
    print("queued " + ", ".join(c["id"] for c in cards) + " on the board "
          "(run `studio.py board` to work them; approve on the board UI)")
    return 0


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    """Queue (or re-queue) the show's Gate 0 card on the board."""
    from .board.gate0 import bootstrap_cards
    from .board.store import BoardSet
    cfg = get_config()
    if not (cfg.show_path(args.show) / "bootstrap.json").exists() and \
            not (cfg.show_path(args.show) / "bible.yaml").exists():
        print(f"error: no show '{args.show}'")
        return 1
    store = BoardSet(cfg)
    cards = [store.upsert(c) for c in bootstrap_cards(args.show, auto=bool(args.auto))]
    store.close()
    print("queued " + ", ".join(c["id"] for c in cards) + " on the board "
          "(run `studio.py board` to work them; approve on the board UI)")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    from .approval import approve_step
    try:
        messages = approve_step(args.show, args.step,
                               char=getattr(args, "char", "") or "",
                               notes=getattr(args, "notes", "") or "")
    except Exception as exc:
        print(f"error: {exc}")
        return 1
    for line in messages:
        print("  ", line)
    print(f"[approve:{args.show}] {args.step} approved.")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    from .approval import reject_step
    try:
        messages = reject_step(args.show, args.step,
                               char=getattr(args, "char", "") or "",
                               notes=getattr(args, "notes", "") or "no notes")
    except Exception as exc:
        print(f"error: {exc}")
        return 1
    for line in messages:
        print("  ", line)
    return 0


# ---------------------------------------------------------------------------
# add-character (scaffold; proposal generation lands at M1)
# ---------------------------------------------------------------------------

def _cmd_add_character(args: argparse.Namespace) -> int:
    cfg = get_config()
    show_dir = cfg.show_path(args.show)
    if not (show_dir / "bible.yaml").exists():
        print(f"error: show '{args.show}' does not exist")
        return 1
    cid = args.name.strip().lower().replace(" ", "-")
    if not cid:
        print("error: --name is required")
        return 2
    target = show_dir / "characters" / f"{cid}.yaml"
    if target.exists():
        print(f"character '{cid}' already exists")
        return 1
    target.write_text(
        "id: {0}\nname: \"{1}\"\nrole: \nappearance_canonical: \"\"\n"
        "appearance_notes: \"\"\npersonality: []\ntraits_for_llm: \"\"\n"
        "voice: {0}_voice\nh3_slot: \"\"\nref_images: []\noutfit_state: {{}}\n"
        "# CharacterProposal generation (AI) lands at M1\n".format(cid, args.name),
        encoding="utf-8",
    )
    print(f"scaffolded character '{cid}' in show '{args.show}' (proposal gen at M1)")
    return 0


# ---------------------------------------------------------------------------
# request-run
# ---------------------------------------------------------------------------

def _cmd_request_run(args: argparse.Namespace) -> int:
    """Create the board cards for the next episode (agent-first)."""
    from .board import request_episode
    from .board.store import BoardSet
    from .show import Show

    cfg = get_config()
    show_id = args.show or (cfg.list_shows() or [None])[0]
    if not show_id:
        print("error: no show exists; run `studio.py init-show --name <id>` first")
        return 1
    store = BoardSet(cfg)
    episode = int(getattr(args, "episode", 0) or 0)
    if episode <= 0:
        continuity = Show(show_id).read_continuity() or {}
        cards = store.all(show_id=show_id)
        episode = max([int(continuity.get("episode") or 0)] +
                      [int(c.get("episode") or 0) for c in cards]) + 1
    cards = request_episode(store, show_id, episode)
    print(f"{show_id} EP{episode:02d}: {len(cards)} card(s) on the board")
    for card in cards:
        print(f"  {card['id']:22s} {card['state']:9s} {card['role']}")
    store.close()
    return 0


# ---------------------------------------------------------------------------
# serve (node B) - broker + consumers + timer source
# ---------------------------------------------------------------------------

def _cmd_board(args: argparse.Namespace) -> int:
    """Run the agent board: HTTP API + supervisor daemon."""
    from .board.api import serve as board_serve
    cfg = get_config()
    port = int(getattr(args, "port", 0) or cfg.get("agents", "port", 8130))
    bind = getattr(args, "bind", "") or cfg.get("agents", "bind", "127.0.0.1")
    start_daemon = not bool(getattr(args, "no_daemon", False))
    cfg.data["agents"]["board_url"] = f"http://{bind}:{port}"
    board_serve(port=port, bind=bind, start_daemon=start_daemon, cfg=cfg)
    print(f"[board] cards + lanes at http://{bind}:{port}/api/board "
          f"(daemon={'on' if start_daemon else 'off'}). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[board] stopping")
    return 0


def _board_url() -> str:
    return (os.environ.get("STUDIO_BOARD_URL")
            or get_config().get("agents", "board_url", "http://127.0.0.1:8130"))


def _get_board(path: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(_board_url().rstrip("/") + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _post_board(path: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _board_url().rstrip("/") + path, data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _cmd_board_tool(args: argparse.Namespace) -> int:
    run_id = args.run or os.environ.get("STUDIO_RUN_ID", "")
    card_id = args.card or os.environ.get("STUDIO_CARD_ID", "")
    if not run_id or not card_id:
        print("error: --run and --card (or STUDIO_RUN_ID/STUDIO_CARD_ID)")
        return 2
    try:
        tool_args = json.loads(args.args or "{}")
    except ValueError as exc:
        print(f"error: --args is not JSON: {exc}")
        return 2
    if not isinstance(tool_args, dict):
        print("error: --args must be a JSON object")
        return 2
    extra = list(getattr(args, "extra", []) or [])
    i = 0
    while i < len(extra):
        token = extra[i]
        if not token.startswith("--"):
            i += 1
            continue
        key = token[2:].replace("-", "_")
        if "=" in key:
            key, value = key.split("=", 1)
            tool_args[key] = value
            i += 1
        elif i + 1 < len(extra) and not extra[i + 1].startswith("--"):
            tool_args[key] = extra[i + 1]
            i += 2
        else:
            tool_args[key] = True
            i += 1
    if isinstance(tool_args.get("ids"), str):
        tool_args["ids"] = [x for x in tool_args["ids"].split(",") if x]
    try:
        out = _post_board(f"/api/tools/{args.name}",
                          {"run_id": run_id, "card_id": card_id,
                           "args": tool_args,
                           "async": bool(getattr(args, "async_", False))},
                          args.timeout)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 1
    if out.get("job_id"):
        print(json.dumps({"job_id": out["job_id"], "state": "running"}))
        return 0
    print(json.dumps(out.get("result"), ensure_ascii=False, default=str))
    return 0


def _cmd_board_wait(args: argparse.Namespace) -> int:
    deadline = time.time() + float(args.timeout or 110)
    while True:
        try:
            out = _get_board(f"/api/jobs/{args.job}")
        except Exception as exc:
            print(f"error: {type(exc).__name__}: {exc}")
            return 1
        if out.get("state") in ("done", "failed") or time.time() >= deadline:
            print(json.dumps(out, ensure_ascii=False, default=str))
            return 0 if out.get("state") != "failed" else 1
        time.sleep(1)


def _cmd_board_finish(args: argparse.Namespace) -> int:
    run_id = args.run or os.environ.get("STUDIO_RUN_ID", "")
    if not run_id:
        print("error: --run (or STUDIO_RUN_ID)")
        return 2
    payload: dict = {"run_id": run_id}
    for key in ("status", "verdict", "summary", "notes", "error"):
        value = getattr(args, key, None)
        if value:
            payload[key] = value
    try:
        _post_board("/api/runs/finish", payload, 60)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 1
    print("finished")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Legacy entry point: now runs the agent board."""
    if getattr(args, "request_once", False):
        from .board import request_episode
        from .board.store import BoardSet
        cfg = get_config()
        store = BoardSet(cfg)
        for show_id in cfg.list_shows():
            cards = request_episode(store, show_id, 1)
            print(f"[board] {show_id}: {len(cards)} card(s) queued")
        store.close()
    return _cmd_board(argparse.Namespace(port=0, bind="", no_daemon=False))


# ---------------------------------------------------------------------------
# renderer (node A) - M0 stub
# ---------------------------------------------------------------------------

def _cmd_renderer(args: argparse.Namespace) -> int:
    print("[renderer] node A H3 renderer agent stub. "
          "ShotScheduled consumption lands at M1. Blocking...")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[renderer] stopping")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    cfg = get_config()
    db = DB(cfg.db_path)
    shows = [args.show] if args.show else cfg.list_shows()
    for show in shows:
        state = db.get_continuity(show)
        runs = db.query(
            "SELECT episode, status, started_at FROM runs WHERE show_id=? "
            "ORDER BY episode DESC LIMIT 5", (show,))
        ep = state.get("episode", 0) if state else 0
        print(f"\nshow: {show}  continuity.episode={ep}")
        if not runs:
            print("  no runs yet")
        for r in runs:
            print(f"  EP{r['episode']:<3} {r['status']:<10} started {r['started_at']}")
    db.close()
    return 0


def _cmd_qc(args: argparse.Namespace) -> int:
    """STT dialogue QC on already-rendered shots (backfill / re-check).

    Every rendered clip in runs/EP##/video/*.mp4 is transcribed and compared
    against its shot's script dialogue. Results persist to
    runs/EP##/qc/summary.json + the qc_results DB table (what the board
    shows). Always re-runs over existing clips; skip already-QC'd shots with
    the ``--shot`` filter to re-check just one.
    """
    from .qc import run_shot_qc
    from .render import _latest_script
    from .show import Show

    cfg = get_config()
    if args.model:
        cfg.data["qc"]["stt"] = {**cfg.get("qc", "stt", {}), "model": args.model}
    show = Show(args.show)
    episodes = [args.episode] if args.episode else sorted(
        p.name for p in (show.dir / "runs").glob("EP*") if p.is_dir()
        and any((p / "video").glob("*.mp4")))
    if not episodes:
        print(f"no rendered clips for {args.show}")
        return 0
    results: dict[str, Any] = {}
    for ep_name in episodes:
        ep = int(str(ep_name).replace("EP", ""))
        script = _latest_script(show, ep)
        if not script:
            continue
        vd = show.dir / "runs" / f"EP{ep:02d}" / "video"
        shots = {s.get("id"): s for sc in script.get("scenes", [])
                 for s in sc.get("shots", [])}
        print(f"\nEP{ep:02d}: {len(list(vd.glob('*.mp4')))} clips")
        for clip in sorted(vd.glob("*.mp4")):
            sid = clip.stem
            if args.shot and sid != args.shot:
                continue
            shot = shots.get(sid, {})
            rep = run_shot_qc(show, ep, clip, shot, cfg=cfg)
            results[sid] = rep
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "skip"}.get(
                rep.get("verdict"), rep.get("verdict"))
            issue = rep.get("issues") and f" — {'; '.join(rep['issues'])[:120]}" or ""
            print(f"  {sid:<14} {mark:<5} {rep.get('score', 0):.0%}{issue}")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_vcheck(args: argparse.Namespace) -> int:
    """Post-render visual check (Qwen3-VL) on already-rendered shots.

    Every rendered clip in runs/EP##/video/*.mp4 is frame-sampled and sent to
    the vision-language model, which verifies the expected characters (vs their
    approved refs) and that the scripted action plays out. Results persist to
    runs/EP##/vcheck/summary.json + the vcheck_results DB table. With
    --auto-feed (default per config vcheck.auto_feed) failures auto-reject into
    the shot gate up to vcheck.max_auto_retakes per shot.
    """
    from .vcheck import run_episode_vcheck
    from .show import Show
    from .render import _latest_script

    cfg = get_config()
    show = Show(args.show)
    episodes = [args.episode] if args.episode else sorted(
        p.name for p in (show.dir / "runs").glob("EP*") if p.is_dir()
        and any((p / "video").glob("*.mp4")))
    if not episodes:
        print(f"no rendered clips for {args.show}")
        return 0
    for ep_name in episodes:
        ep = int(str(ep_name).replace("EP", ""))
        if not _latest_script(show, ep):
            continue
        print(f"\nEP{ep:02d}: visual check…")
        feed = None
        if getattr(args, "no_auto_feed", False):
            feed = False
        elif getattr(args, "auto_feed", False):
            feed = True
        actions = run_episode_vcheck(show, ep, cfg=cfg, auto_feed=feed)
        for a in actions or []:
            print(f"  {a}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studio.py", description="Anime Studio CLI")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="health-check config/db/board/clients")

    st = sub.add_parser("setup", help="per-machine provisioning checklist (DESIGN.md §7.7)")
    st.add_argument("--venv", action="store_true", help="create the .venv")
    st.add_argument("--install", action="store_true", help="pip install -e . into .venv")
    st.add_argument("--lmstudio", action="store_true",
                    help="provision LM Studio (ensure server + model loaded)")
    st.add_argument("--remote", default="", help="role (worker|renderer) or host[:port] to target")

    lms = sub.add_parser("lms", help="control LM Studio (locally or via --remote)")
    lms.add_argument("op", choices=["start", "load", "unload", "status", "get"])
    lms.add_argument("--model", default="", help="model key for load/get")
    lms.add_argument("--gpu-ratio", default=None, help="'max' | 'off' | 0..1")
    lms.add_argument("--remote", default="")

    cfy = sub.add_parser("comfy", help="start/stop the node's portable ComfyUI")
    cfy.add_argument("op", choices=["start", "stop"])
    cfy.add_argument("--which", default="", choices=["krea2", "h3"])
    cfy.add_argument("--remote", default="")

    ra = sub.add_parser("remote-agent", help="run this node's control agent (blocking)")
    ra.add_argument("--port", type=int, default=None)
    ra.add_argument("--bind", default=None)
    ra.add_argument("--token", default=None)

    doc = sub.add_parser("doctor", help="run verify on a remote node")
    doc.add_argument("--remote", required=True, help="role (worker|renderer) or host[:port]")

    kf = sub.add_parser("keyframe", help="generate a Krea 2 keyframe image")
    kf.add_argument("--prompt", required=True)
    kf.add_argument("--seed", type=int, default=0)
    kf.add_argument("--aspect", default="16:9", choices=["1:1","3:2","4:3","16:9","9:16","21:9"])
    kf.add_argument("--out", default="", help="output path on the node")
    kf.add_argument("--lora", action="store_true", help="enable the fedor_bypass LoRA")
    kf.add_argument("--remote", default="")

    h3s = sub.add_parser("h3status", help="show H3 model download progress on node A")

    sh = sub.add_parser("shot", help="render an H3 shot via the local Director workflow")
    sh.add_argument("--prompt", required=True, help="global_prompt for the shot")
    sh.add_argument("--segment", default="", help="optional per-segment prompt")
    sh.add_argument("--duration", type=float, default=5.167)
    sh.add_argument("--seed", type=int, default=1)
    sh.add_argument("--steps", type=int, default=None,
                    help="sampling steps (default: config comfy.h3.steps)")
    sh.add_argument("--spectrum", dest="spectrum", action="store_true", default=None,
                    help="enable Spectrum forecast acceleration")
    sh.add_argument("--no-spectrum", dest="spectrum", action="store_false",
                    help="disable Spectrum")
    sh.add_argument("--fbc", dest="fbc", action="store_true", default=None,
                    help="enable FirstBlockCache")
    sh.add_argument("--no-fbc", dest="fbc", action="store_false",
                    help="disable FirstBlockCache")
    sh.add_argument("--out", default="")
    sh.add_argument("--timeout", type=float, default=1800.0)
    sh.add_argument("--ref-image", action="append", default=[],
                    help="local image path to use as a reference (repeatable; enables ref2va)")

    ex = sub.add_parser("extend", help="extend an existing H3 render with a new prompt "
                                       "(Director Retake Mode)")
    ex.add_argument("--video", required=True, help="path to the existing render (base video)")
    ex.add_argument("--prompt", required=True, help="prompt for the new content")
    ex.add_argument("--duration", type=float, default=5.0, help="extension length in seconds")
    ex.add_argument("--start", default="end",
                    help="start frame of the retake range; 'end' = append after the video")
    ex.add_argument("--seed", type=int, default=1)
    ex.add_argument("--steps", type=int, default=None,
                    help="sampling steps (default: config comfy.h3.steps)")
    ex.add_argument("--spectrum", dest="spectrum", action="store_true", default=None,
                    help="enable Spectrum forecast acceleration")
    ex.add_argument("--no-spectrum", dest="spectrum", action="store_false",
                    help="disable Spectrum")
    ex.add_argument("--fbc", dest="fbc", action="store_true", default=None,
                    help="enable FirstBlockCache")
    ex.add_argument("--no-fbc", dest="fbc", action="store_false",
                    help="disable FirstBlockCache")
    ex.add_argument("--out", default="")
    ex.add_argument("--timeout", type=float, default=1800.0)

    sm = sub.add_parser("scene-master", help="render one scene as a seamless multishot master (H3Multishot)")
    sm.add_argument("--show", required=True, help="show id")
    sm.add_argument("--episode", type=int, default=1)
    sm.add_argument("--scene", required=True, help="scene id, e.g. s01")
    sm.add_argument("--seed", type=int, default=1)

    rv = sub.add_parser("review", help="run the Stage 2a writers'-room reviewers on a script")
    rv.add_argument("--script", required=True, help="path to a script.json")

    ln = sub.add_parser("learn", help="aggregate writers'-room review notes into durable prompt rules")
    ln.add_argument("--show", required=True, help="show id")
    ln.add_argument("--episode", type=int, required=True)
    ln.add_argument("--round", type=int, default=None, help="review round (default: latest per reviewer)")
    ln.add_argument("--all", action="store_true", help="include MEDIUM/LOW, not just HIGH")
    ln.add_argument("--promote", action="store_true",
                    help="append HIGH-derived rules to director_notes.json")

    rw = sub.add_parser("rewrite", help="one-off rewrite with a chosen model, then re-review")
    rw.add_argument("--show", required=True)
    rw.add_argument("--episode", type=int, default=1)
    rw.add_argument("--stage", choices=["script", "outline"], default="script",
                    help="what to rewrite: the failed script (default) or the outline")
    rw.add_argument("--model", required=True, help="LM Studio model id")
    rw.add_argument("--notes", default="", help="extra director note folded into the rewrite")

    ev = sub.add_parser("eval", help="run a stage N times on frozen inputs; measure variance/quality/cost")
    ev.add_argument("--show", required=True, help="show id")
    ev.add_argument("--episode", type=int, default=1)
    ev.add_argument("--stage", choices=["outline", "prose"], default="prose")
    ev.add_argument("--runs", type=int, default=5)
    ev.add_argument("--temperature", type=float, default=None,
                    help="override the sampler temperature (else the stage's own)")
    ev.add_argument("--no-quality", action="store_true",
                    help="skip the reviewer pass (variance/cost only)")
    ev.add_argument("--label", default="")
    ev.add_argument("--out", default=None, help="output dir (default evals/<show>_ep<NN>_<stage>_<ts>)")

    evc = sub.add_parser("eval-compare", help="compare two eval report.json files")
    evc.add_argument("a", help="baseline report.json")
    evc.add_argument("b", help="candidate report.json")

    rv.add_argument("--show", default="", help="show id (for continuity + mature_spec context)")
    rv.add_argument("--episode", type=int, default=1)
    rv.add_argument("--round", type=int, default=1)

    sc = sub.add_parser("script", help="generate an episode script (prose -> review -> shots)")
    sc.add_argument("--show", required=True)
    sc.add_argument("--episode", type=int, default=1)

    pr = sub.add_parser("prose", help="prose-first episode: whole screenplay, review, then shots by need")
    pr.add_argument("--show", required=True)
    pr.add_argument("--episode", type=int, default=1)
    pr.add_argument("--max-revisions", type=int, default=2)
    pr.add_argument("--breakdown", action="store_true",
                    help="after prose passes, cut it into shots")

    du = sub.add_parser("dump", help="print EVERYTHING for an episode (full text, no truncation)")
    du.add_argument("--show", required=True)
    du.add_argument("--episode", type=int, required=True)

    i = sub.add_parser("init-show", help="scaffold a new show + generate the concept")
    i.add_argument("--name", required=True, help="show slug/name, e.g. neon-sutra")
    i.add_argument("--brief", default="", help="one-line creative brief")
    i.add_argument("--generate", action="store_true", help="zero-input: run the whole Gate 0 chain automatically")

    b = sub.add_parser("bootstrap", help="advance the Gate 0 chain (Concept->Bible->Characters->Scenes)")
    b.add_argument("--show", required=True)
    b.add_argument("--auto", action="store_true", help="auto-approve every step to completion")

    ap = sub.add_parser("approve", help="approve a bootstrap artifact (human gate)")
    ap.add_argument("--show", required=True)
    ap.add_argument("--step", required=True, choices=["concept", "bible", "character", "voice", "scenes"])
    ap.add_argument("--char", default="", help="character name (for --step character|voice)")

    rj = sub.add_parser("reject", help="reject a bootstrap artifact with notes (regenerates it)")
    rj.add_argument("--show", required=True)
    rj.add_argument("--step", required=True, choices=["concept", "bible", "character", "voice", "scenes"])
    rj.add_argument("--char", default="")
    rj.add_argument("--notes", default="")

    a = sub.add_parser("add-character", help="scaffold a character into a show")
    a.add_argument("--show", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--brief", default="")

    r = sub.add_parser("request-run", help="create board cards for the next episode")
    r.add_argument("--show", default="")
    r.add_argument("--episode", type=int, default=0,
                   help="episode number (default: next after the board/continuity)")
    r.add_argument("--mode", default="overnight")
    r.add_argument("--budget-min", type=int, default=480)

    b = sub.add_parser("board", help="run the agent board (API + supervisor daemon)")
    b.add_argument("--port", type=int, default=0, help="API port (config agents.port)")
    b.add_argument("--bind", default="", help="bind address (config agents.bind)")
    b.add_argument("--no-daemon", action="store_true",
                   help="serve the API only; do not launch agents")

    bt = sub.add_parser("board-tool", help="run one board tool for an agent run")
    bt.add_argument("--name", required=True)
    bt.add_argument("--run", default="")
    bt.add_argument("--card", default="")
    bt.add_argument("--args", default="{}", help="tool args as JSON")
    bt.add_argument("--timeout", type=float, default=3600)
    bt.add_argument("--async", dest="async_", action="store_true",
                    help="submit and return a job id; poll with board-wait")
    bw = sub.add_parser("board-wait", help="wait for an async board tool job")
    bw.add_argument("--job", required=True)
    bw.add_argument("--timeout", type=float, default=110)

    bf = sub.add_parser("board-finish", help="send an agent run's final verdict")
    bf.add_argument("--run", default="")
    bf.add_argument("--status", default="")
    bf.add_argument("--verdict", default="")
    bf.add_argument("--summary", default="")
    bf.add_argument("--notes", nargs="?", const="", default="")
    bf.add_argument("--error", default="")

    s = sub.add_parser("serve", help="run the agent board (legacy alias)")
    s.add_argument("--request-once", action="store_true",
                   help="queue episode 1 cards for each show, then run the board")

    sub.add_parser("renderer", help="run the renderer node A agent (M0 stub)")

    st = sub.add_parser("status", help="show per-show run status")
    st.add_argument("--show", default="")

    qc = sub.add_parser("qc", help="run STT dialogue QC on rendered shots "
                                   "(garbled-audio detection)")
    qc.add_argument("--show", required=True, help="show id")
    qc.add_argument("--episode", type=int, default=None,
                    help="episode number (default: all rendered episodes)")
    qc.add_argument("--shot", default="", help="only QC this shot id (default: all)")
    qc.add_argument("--model", default="", help="override config qc.stt.model")
    qc.add_argument("--json", action="store_true", help="print a JSON rollup")

    vc = sub.add_parser("vcheck", help="post-render visual QC (Qwen3-VL) on rendered shots")
    vc.add_argument("--show", required=True, help="show id")
    vc.add_argument("--episode", type=int, default=None,
                    help="only check this episode (default: all with clips)")
    vc.add_argument("--no-auto-feed", action="store_true",
                    help="do NOT auto-reject failures into the shot gate")
    vc.add_argument("--auto-feed", action="store_true",
                    help="force auto-reject (overrides config vcheck.auto_feed)")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        if args.command == "board-tool":
            args.extra = list(getattr(args, "extra", []) or []) + unknown
        else:
            parser.error("unrecognized arguments: " + " ".join(unknown))
    _log_setup(args.verbose)
    handlers = {
        "verify": _cmd_verify,
        "setup": _cmd_setup,
        "lms": _cmd_lms,
        "comfy": _cmd_comfy,
        "remote-agent": _cmd_remote_agent,
        "doctor": _cmd_doctor,
        "keyframe": _cmd_keyframe,
        "review": _cmd_review,
        "learn": _cmd_learn,
        "rewrite": _cmd_rewrite,
        "eval": _cmd_eval,
        "eval-compare": _cmd_eval_compare,
        "prose": _cmd_prose,
        "dump": _cmd_dump,
        "script": _cmd_script,
        "h3status": _cmd_h3status,
        "shot": _cmd_shot,
        "extend": _cmd_extend,
        "scene-master": _cmd_scene_master,
        "init-show": _cmd_init_show,
        "bootstrap": _cmd_bootstrap,
        "approve": _cmd_approve,
        "reject": _cmd_reject,
        "add-character": _cmd_add_character,
        "request-run": _cmd_request_run,
        "board": _cmd_board,
        "board-tool": _cmd_board_tool,
        "board-wait": _cmd_board_wait,
        "board-finish": _cmd_board_finish,
        "serve": _cmd_serve,
        "renderer": _cmd_renderer,
        "status": _cmd_status,
        "qc": _cmd_qc,
        "vcheck": _cmd_vcheck,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
