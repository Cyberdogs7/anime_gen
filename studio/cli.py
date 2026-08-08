"""Command-line interface. See DESIGN.md §17 / §20.1.

Commands: verify | init-show | add-character | request-run | serve | renderer | status
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import yaml

from .bus import make_broker
from .bus.events import RUN_REQUESTED, RUN_STARTED, new_event
from .config import Config, get_config
from .db import DB
from .run_controller import RunController
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
    from .clients.lmstudio import LMStudioClient
    from .scriptgen import WritersRoom
    from .show import Show
    show = Show(args.show)
    llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=900)
    result = WritersRoom(show, llm=llm).run(args.episode, synopsis=args.synopsis,
                                            max_revisions=args.max_revisions)
    print(f"episode {args.episode}: rounds={result['rounds']} passed={result['passed']}")
    for name, r in result["reviews"].items():
        print(f"  [{'PASS' if r['pass'] else 'FAIL'}] {name:12s} score={r.get('score')} "
              f"notes={len(r.get('notes', []))}")
    p = show.dir / "runs" / f"EP{args.episode:02d}" / f"script.r{result['rounds']}.json"
    print("script:", p)
    return 0 if result["passed"] else 1


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


def _cmd_review(args: argparse.Namespace) -> int:
    from .clients.lmstudio import LMStudioClient
    from .review import all_pass, run_reviewers
    from .show import Show
    script = json.loads(Path(args.script).read_text(encoding="utf-8-sig"))
    show = Show(args.show) if args.show else None
    llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=600)
    results = run_reviewers(script, show=show, llm=llm, episode=args.episode, round_no=args.round)
    for name, r in results.items():
        n = len(r.get("notes", []))
        print(f"{'[ PASS ]' if r['pass'] else '[ FAIL ]'} {name:12s} score={r.get('score')} notes={n}")
        for note in r.get("notes", [])[:3]:
            text = note.get("note") if isinstance(note, dict) else str(note)
            print(f"    - {text}")
    print("ALL PASS" if all_pass(results) else "REVISION REQUIRED")
    return 0 if all_pass(results) else 1


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
            "runtime_target_s": cfg.get("show_profile", "runtime_target_s", 1320),
            "style_guide": "",  # filled by the Gate 0 bootstrap
            "quality_baseline": cfg.get("show_profile", "baseline", "ranma-1-2"),
        },
        "arcs": [],
        "overall_plotline": None,
        "content_policy": cfg.get("show_profile", "maturity", "mature"),
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

    # Gate 0: generate the concept (brief or zero-input auto bootstrap)
    if args.brief or args.generate:
        from .bootstrap import BootstrapChain
        from .show import Show
        chain = BootstrapChain(Show(show_id))
        if args.generate:
            chain._auto_override = True
            print(f"[init-show:{show_id}] running zero-input auto bootstrap...")
        else:
            print(f"[init-show:{show_id}] generating concept from brief...")
        for line in chain.advance(brief=args.brief):
            print("  ", line)
        state = Show(show_id).bootstrap_state()
        if state.get("complete"):
            print(f"[init-show:{show_id}] BOOTSTRAP COMPLETE - the show is cast and can schedule episodes.")
        else:
            print(f"[init-show:{show_id}] next: `studio.py bootstrap --show {show_id}` "
                  "(or `--auto`) to advance the approval chain.")
    return 0


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    from .bootstrap import BootstrapChain
    from .show import Show
    chain = BootstrapChain(Show(args.show))
    if args.auto:
        chain._auto_override = True
    log = chain.advance()
    for line in log:
        print("  ", line)
    state = Show(args.show).bootstrap_state()
    if state.get("complete"):
        print(f"[bootstrap:{args.show}] BOOTSTRAP COMPLETE")
    else:
        pending = _pending_step(state)
        print(f"[bootstrap:{args.show}] blocked on human gate: {pending or 'unknown'}")
        print(f"  approve: `studio.py approve --show {args.show} --step {pending.split(' ')[0] if pending else '...'}`")
    return 0


def _pending_step(state: dict) -> str | None:
    if state.get("concept", {}).get("status") == "pending":
        return "concept"
    if state.get("bible", {}).get("status") == "pending":
        return "bible"
    for ch in state.get("characters", []):
        if ch.get("voice") == "pending":
            return f"voice --char {ch.get('name')}"
        if ch.get("proposal") == "pending":
            return f"character --char {ch.get('name')}"
    if state.get("scenes", {}).get("status") == "pending":
        return "scenes"
    return None


def _cmd_approve(args: argparse.Namespace) -> int:
    from .bootstrap import BootstrapChain
    from .bus.events import (
        BIBLE_APPROVED, CONCEPT_APPROVED, SCENE_REGISTRY_APPROVED,
        VOICE_APPROVED, new_event,
    )
    from .show import Show
    show = Show(args.show)
    st = show.bootstrap_state()
    step = args.step
    if step == "concept":
        st["concept"]["status"] = "approved"
        show.set_bootstrap_state(st)
        chain = BootstrapChain(show)
        chain._emit(new_event(CONCEPT_APPROVED, show_id=args.show, payload={"manual": True}))
    elif step == "bible":
        st["bible"]["status"] = "approved"
        show.set_bootstrap_state(st)
        chain = BootstrapChain(show)
        chain._emit(new_event(BIBLE_APPROVED, show_id=args.show, payload={"manual": True}))
    elif step in ("character", "voice"):
        name = args.char or ""
        entry = next((c for c in st.get("characters", []) if c.get("name") == name), None)
        if entry is None:
            print(f"error: no character named '{name}' in bootstrap state")
            return 1
        entry["voice"] = "approved"
        show.set_bootstrap_state(st)
        chain = BootstrapChain(show)
        chain._emit(new_event(VOICE_APPROVED, show_id=args.show,
                              payload={"char": name, "manual": True}))
    elif step == "scenes":
        st["scenes"]["status"] = "approved"
        show.set_bootstrap_state(st)
        chain = BootstrapChain(show)
        chain._emit(new_event(SCENE_REGISTRY_APPROVED, show_id=args.show, payload={"manual": True}))
    else:
        print(f"error: unknown step '{step}' (concept|bible|character|voice|scenes)")
        return 2
    for line in chain.advance():
        print("  ", line)
    print(f"[approve:{args.show}] {step} approved.")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    from .bootstrap import BootstrapChain
    from .show import Show
    show = Show(args.show)
    st = show.bootstrap_state()
    step = args.step
    notes = args.notes or "no notes"
    if step == "concept":
        st["concept"] = {"status": "", "rejected_notes": notes}
    elif step == "bible":
        st["bible"] = {"status": "", "rejected_notes": notes}
    elif step in ("character", "voice"):
        name = args.char or ""
        entry = next((c for c in st.get("characters", []) if c.get("name") == name), None)
        if entry is None:
            print(f"error: no character named '{name}' in bootstrap state")
            return 1
        # reject regenerates the proposal: drop the character file + reset its entry
        cid = name.lower().replace(" ", "-")
        for c in show.list_characters():
            if show.read_character(c).get("name") == name:
                cid = c
                break
        char_file = show.characters_dir / f"{cid}.yaml"
        if char_file.exists():
            char_file.unlink()
        st["characters"] = [c for c in st.get("characters", []) if c.get("name") != name]
        st["characters"].append({"name": name, "proposal": "pending", "refs": "",
                                 "voice": "", "rejected_notes": notes})
    elif step == "scenes":
        st["scenes"] = {"status": "", "rejected_notes": notes}
    else:
        print(f"error: unknown step '{step}'")
        return 2
    show.set_bootstrap_state(st)
    print(f"[reject:{args.show}] {step} rejected with notes: {notes!r}; regenerating...")
    chain = BootstrapChain(show)
    for line in chain.advance():
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
    cfg = get_config()
    db = DB(cfg.db_path)
    bus = make_broker(cfg["bus"])
    controller = RunController(cfg, db, bus)
    bus.register("bus:run", "run-controller", controller.on_run_requested)
    show_id = args.show or (cfg.list_shows() or [None])[0]
    if not show_id:
        print("error: no show exists; run `studio.py init-show --name <id>` first")
        return 1
    bus.publish("bus:run", new_event(
        RUN_REQUESTED, show_id=show_id,
        payload={"mode": args.mode, "budget_min": args.budget_min},
    ))
    row = db.query(
        "SELECT id, episode, status FROM runs WHERE show_id=? ORDER BY id DESC LIMIT 1",
        (show_id,),
    )[0]
    print(f"run #{row['id']} for {show_id} EP{row['episode']} status={row['status']}")
    db.close()
    return 0


# ---------------------------------------------------------------------------
# serve (node B) - broker + consumers + timer source
# ---------------------------------------------------------------------------

def _cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import serve
    httpd = serve(port=args.port, bind=args.bind)
    url = f"http://{args.bind}:{args.port}"
    print(f"[dashboard] approval & review dashboard at {url}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopping")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    cfg = get_config()
    db = DB(cfg.db_path)
    bus = make_broker(cfg["bus"])
    controller = RunController(cfg, db, bus)

    bus.register("bus:run", "run-controller", controller.on_run_requested)
    bus.register("bus:run", "log-run", lambda e: log.info("run event: %s", e.type))

    def _log_stage(e):
        log.info("stage event %s (show=%s ep=%s)", e.type, e.show_id, e.episode)

    for stream in ("bus:dev", "bus:script", "bus:keyframes", "bus:shots",
                   "bus:dialogue", "bus:assembly", "bus:qc", "bus:delivery",
                   "bus:approval", "bus:system"):
        bus.register(stream, "log", _log_stage)

    bus.start()
    print(f"[serve] broker={cfg['bus']['provider']} nodes: worker-B. Ctrl+C to stop.")
    shows = cfg.list_shows()
    if not shows:
        print("[serve] no shows yet - `studio.py init-show --name <id>`")
    elif args.request_once:
        for show in shows:
            bus.publish("bus:run", new_event(
                RUN_REQUESTED, show_id=show,
                payload={"mode": cfg.get("pipeline", "mode", "overnight"),
                         "budget_min": cfg.get("pipeline", "budget_min", 480)},
            ))
            print(f"[serve] published RunRequested for {show}")
        print("[serve] stage consumers land at M1; blocking for demo. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[serve] stopping")
    finally:
        bus.stop()
        db.close()
    return 0


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


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studio.py", description="Anime Studio CLI")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="health-check config/db/bus/clients")

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

    rv = sub.add_parser("review", help="run the Stage 2a writers'-room reviewers on a script")
    rv.add_argument("--script", required=True, help="path to a script.json")

    dash = sub.add_parser("dashboard", help="run the local human approval & review dashboard")
    dash.add_argument("--port", type=int, default=8125)
    dash.add_argument("--bind", default="127.0.0.1")
    rv.add_argument("--show", default="", help="show id (for continuity + mature_spec context)")
    rv.add_argument("--episode", type=int, default=1)
    rv.add_argument("--round", type=int, default=1)

    sc = sub.add_parser("script", help="generate an episode script via the writers' room")
    sc.add_argument("--show", required=True)
    sc.add_argument("--episode", type=int, default=1)
    sc.add_argument("--synopsis", default="", help="episode synopsis (default: next arc beat)")
    sc.add_argument("--max-revisions", type=int, default=None)

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

    r = sub.add_parser("request-run", help="publish RunRequested for a show")
    r.add_argument("--show", default="")
    r.add_argument("--mode", default="overnight")
    r.add_argument("--budget-min", type=int, default=480)

    s = sub.add_parser("serve", help="run the worker node B (broker + consumers + timer)")
    s.add_argument("--request-once", action="store_true",
                   help="publish RunRequested for each show once, then block (M0 demo)")

    sub.add_parser("renderer", help="run the renderer node A agent (M0 stub)")

    st = sub.add_parser("status", help="show per-show run status")
    st.add_argument("--show", default="")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        "script": _cmd_script,
        "h3status": _cmd_h3status,
        "shot": _cmd_shot,
        "extend": _cmd_extend,
        "init-show": _cmd_init_show,
        "bootstrap": _cmd_bootstrap,
        "approve": _cmd_approve,
        "reject": _cmd_reject,
        "add-character": _cmd_add_character,
        "request-run": _cmd_request_run,
        "serve": _cmd_serve,
        "dashboard": _cmd_dashboard,
        "renderer": _cmd_renderer,
        "status": _cmd_status,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
