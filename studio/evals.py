"""Prompt/stage evaluation harness: run one pipeline stage N times against a
frozen input snapshot and measure how much the output VARIES and how GOOD it is.

Why this exists: prompt changes were being argued about, not measured. The
reported problem ("the prose prompt gives 99% the same script every run") is a
variance question, and "is this prompt better?" is a quality question. This
harness turns both into numbers you can diff.

Design
------
* One stage per experiment (``outline`` -> generate_episode_plan, ``prose`` ->
  generate_episode_prose). Add more in ``_STAGES``.
* ISOLATION: the show's config (bible, cast, scenes, continuity, seasons,
  director notes; plus the approved plan for the prose stage) is copied ONCE into
  the experiment folder, and the scratch show is rebuilt from that frozen copy
  BEFORE EVERY run. So every run sees byte-identical inputs and the ONLY variable
  is the model's sampling — exactly the thing under test. Any state the stage
  mutates (continuity, season, plan) is discarded and reset between runs.
* The real client runs (local/zen routing, real temperature) so the measurement
  reflects production. ``--temperature`` overrides the sampler WITHOUT editing the
  stage code, for temperature sweeps.
* Metrics: variance (pairwise text similarity across runs), quality (the
  existing writers'-room reviewers' scores/notes), and cost (LLM calls, wall
  time, provider mix).

Outputs (self-contained, under ``evals/``):
  <exp>/report.json     full machine-readable report
  <exp>/metrics.csv     one row per run (structural + quality + cost)
  <exp>/run_<i>/        the artifact, prompt logs, review notes, scratch show

CLI:
  python studio.py eval --show <id> --episode 1 --stage prose --runs 5
  python studio.py eval --show <id> --episode 1 --stage outline --runs 5
  python studio.py eval-compare evals/A/report.json evals/B/report.json
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_config
from .show import Show

STAGES = ("outline", "prose")
_ARTIFACT = {"outline": "plan.json", "prose": "episode_prose.txt"}
# Directories under a show that are heavy/derived and never inputs to the stages
# we evaluate against a frozen snapshot.
_SKIP_DIRS = {"runs", "assets", "video", "storyboard", "objects", "reviews",
              "qc", "vcheck", "refs", "archive", "cache", "prompts"}
_SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm",
                ".wav", ".mp3", ".m4a", ".glb"}


# ---------------------------------------------------------------------------
# Text / metric helpers (deterministic, stdlib only)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if (ta or tb) else 1.0


def similarity(a: str, b: str) -> dict[str, float]:
    """Two independent similarity signals; 1.0 == identical."""
    na, nb = _normalize(a), _normalize(b)
    if not na and not nb:
        ratio = 1.0
    else:
        ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
    return {"ratio": round(ratio, 4), "jaccard": round(_jaccard(a, b), 4)}


def structural(text: str) -> dict[str, int]:
    words = _normalize(text).split()
    return {
        "chars": len(text or ""),
        "words": len(words),
        "unique_words": len(set(words)),
    }


def variance(artifacts: list[str]) -> dict[str, Any]:
    """Pairwise variance across N outputs of the same stage/inputs."""
    n = len(artifacts)
    if n < 2:
        return {"runs": n, "pairs": 0, "similarity_mean": None,
                "similarity_min": None, "similarity_max": None,
                "jaccard_mean": None, "diversity": None}
    ratios, jacs = [], []
    for i in range(n):
        for j in range(i + 1, n):
            s = similarity(artifacts[i], artifacts[j])
            ratios.append(s["ratio"])
            jacs.append(s["jaccard"])
    mean_ratio = statistics.fmean(ratios)
    return {
        "runs": n,
        "pairs": len(ratios),
        "similarity_mean": round(mean_ratio, 4),
        "similarity_min": round(min(ratios), 4),
        "similarity_max": round(max(ratios), 4),
        "jaccard_mean": round(statistics.fmean(jacs), 4),
        "diversity": round(1.0 - mean_ratio, 4),
    }


# ---------------------------------------------------------------------------
# Stage adapters — run one stage, return the artifact text + a quality verdict
# ---------------------------------------------------------------------------

def _reviews_quality(reviews: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not reviews:
        return {}
    passed = sum(1 for v in reviews.values() if v.get("pass"))
    failing = [r for r, v in reviews.items() if v.get("pass") is False]
    return {
        "reviewers": len(reviews),
        "pass_rate": round(passed / len(reviews), 3),
        "failing": failing,
        "by_reviewer": {r: {"grade": v.get("grade"), "pass": bool(v.get("pass"))}
                        for r, v in reviews.items()},
    }


def _run_outline(show: Show, episode: int, llm, cfg, quality: bool,
                 run_dir: Path) -> dict[str, Any]:
    from .planner import generate_episode_plan
    plan = generate_episode_plan(show, episode, llm=llm, cfg=cfg)
    artifact = json.dumps(plan, indent=2, ensure_ascii=False)
    q: dict[str, Any] = {}
    if quality:
        pr = plan.get("plan_review") or {}
        revs = pr.get("reviews") or {}
        q = {
            "passed": bool(pr.get("passed")),
            "rounds": pr.get("rounds"),
            "failing": [r for r, v in revs.items()
                        if str(v.get("grade", "")).lower() != "pass"],
        }
    return {"artifact": artifact, "quality": q,
            "extra": {"scenes": len(plan.get("scenes") or []),
                      "episode_type": plan.get("episode_type", ""),
                      "title": plan.get("title", "")}}


def _run_prose(show: Show, episode: int, llm, cfg, quality: bool,
               run_dir: Path) -> dict[str, Any]:
    from .prose import generate_episode_prose, review_episode_prose
    text = generate_episode_prose(show, episode, llm=llm, cfg=cfg)
    q: dict[str, Any] = {}
    if quality:
        reviews = review_episode_prose(show, episode, text, llm=llm, cfg=cfg,
                                       round_no=1, notes_dir=run_dir / "reviews")
        q = _reviews_quality(reviews)
    return {"artifact": text, "quality": q, "extra": {}}


_STAGES = {"outline": _run_outline, "prose": _run_prose}


# ---------------------------------------------------------------------------
# LLM call accounting (counts calls, wall time, provider mix; optional temp override)
# ---------------------------------------------------------------------------

class _CountingLLM:
    """Transparent proxy around the real client that tallies usage.

    ``temperature`` (when not None) overrides the sampler on every call so a
    temperature sweep needs no change to the stage code.
    """

    def __init__(self, client, temperature: float | None = None):
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "temperature", temperature)
        object.__setattr__(self, "calls", 0)
        object.__setattr__(self, "seconds", 0.0)
        object.__setattr__(self, "providers", {})
        object.__setattr__(self, "models", {})

    def _record(self) -> None:
        self.calls += 1
        info = getattr(self.client, "provider_info", None)
        detail = info() if callable(info) else None
        if isinstance(detail, dict):
            p = detail.get("provider")
            m = detail.get("model")
            if p:
                self.providers[p] = self.providers.get(p, 0) + 1
            if m:
                self.models[m] = self.models.get(m, 0) + 1

    def _call(self, name: str, messages, **kwargs):
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        t0 = time.time()
        try:
            return getattr(self.client, name)(messages, **kwargs)
        finally:
            self.seconds += time.time() - t0
            self._record()

    def chat(self, messages, **kwargs):
        return self._call("chat", messages, **kwargs)

    def chat_json(self, messages, **kwargs):
        return self._call("chat_json", messages, **kwargs)

    def __getattr__(self, name):
        return getattr(self.client, name)


# ---------------------------------------------------------------------------
# Frozen snapshot + scratch rebuild
# ---------------------------------------------------------------------------

def _copy_show(src: Path, dst: Path) -> None:
    def _ignore(dirname: str, names: list[str]) -> set[str]:
        skip: set[str] = set()
        for n in names:
            if n in _SKIP_DIRS:
                skip.add(n)
            elif Path(n).suffix.lower() in _SKIP_SUFFIX:
                skip.add(n)
        return skip

    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=_ignore)


def _input_fingerprint(show: Show, episode: int, stage: str,
                       plan_src: Path | None) -> dict[str, Any]:
    """A hash of the exact frozen inputs, so two reports are comparable."""
    parts: list[str] = []
    bible = show.read_bible()
    parts.append(json.dumps(bible, sort_keys=True, ensure_ascii=False))
    for cid in show.list_characters():
        parts.append(json.dumps(show.read_character(cid), sort_keys=True, ensure_ascii=False))
    parts.append(json.dumps(show.read_continuity(), sort_keys=True, ensure_ascii=False))
    for extra in ("director_notes.json", "concept.json"):
        p = show.dir / extra
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    if plan_src and plan_src.exists():
        parts.append(plan_src.read_text(encoding="utf-8"))
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return {"hash": digest, "bible_title": bible.get("title", ""),
            "characters": len(show.list_characters()), "stage": stage,
            "episode": episode}


def _prepare_stage_stage(show: Show, episode: int, stage: str,
                         plan_src: Path | None, llm, cfg) -> None:
    """Best-effort: materialise prerequisites that would otherwise vary per run.

    For the outline stage, ensure the season file exists so season generation does
    not become a hidden variance source inside the outline measurement.
    """
    if stage != "outline":
        return
    try:
        from .season import ensure_season
        ensure_season(show, episode, llm=llm, cfg=cfg)
    except Exception:
        pass  # absent season is acceptable; it just adds noise to the outline


# ---------------------------------------------------------------------------
# The experiment
# ---------------------------------------------------------------------------

def run_eval(show_id: str, episode: int = 1, stage: str = "prose", runs: int = 5,
             cfg=None, llm=None, temperature: float | None = None,
             quality: bool = True, out_dir: Path | str | None = None,
             label: str = "", progress=print) -> dict[str, Any]:
    """Run one stage ``runs`` times against a frozen snapshot; write the report.

    Returns the report dict. ``llm`` may be injected (tests); otherwise a real
    client is created and the stage runs for real.
    """
    cfg = cfg or get_config()
    if stage not in _STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    real_show = Show(show_id, root=cfg.root / "shows" / show_id)
    if not real_show.dir.exists():
        raise FileNotFoundError(f"no such show: {real_show.dir}")

    plan_src: Path | None = None
    if stage == "prose":
        plan_src = real_show.dir / "runs" / f"EP{episode:02d}" / "plan.json"
        if not plan_src.exists():
            raise FileNotFoundError(
                f"stage=prose needs an approved plan at {plan_src}. "
                f"Generate the episode (e.g. `python studio.py bootstrap/reconcile` "
                f"or the plan step) first, then re-run the eval.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if out_dir is None:
        slug = f"{show_id}_ep{episode:02d}_{stage}_{stamp}"
        out_dir = cfg.root / "evals" / slug
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) freeze the inputs once
    frozen = out_dir / "frozen"
    _copy_show(real_show.dir, frozen)
    if plan_src is not None:
        ep_runs = frozen / "runs" / f"EP{episode:02d}"
        ep_runs.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan_src, ep_runs / "plan.json")
    frozen_show = Show(show_id, root=frozen)

    if llm is None:
        from .clients.lmstudio import LMStudioClient
        llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=600)
    _prepare_stage_stage(frozen_show, episode, stage, plan_src, llm, cfg)
    fingerprint = _input_fingerprint(frozen_show, episode, stage, plan_src)

    runner = _STAGES[stage]
    artifact_name = _ARTIFACT[stage]
    run_reports: list[dict[str, Any]] = []
    artifacts: list[str] = []

    progress(f"[eval] {show_id} EP{episode:02d} stage={stage} runs={runs} "
             f"temp={'stage-default' if temperature is None else temperature} "
             f"inputs={fingerprint['hash']}")

    for i in range(1, runs + 1):
        run_dir = out_dir / f"run_{i:02d}"
        # Rebuild the scratch show from the frozen snapshot: every run starts from
        # identical inputs regardless of what the previous run mutated.
        scratch = run_dir / "show"
        run_dir.mkdir(parents=True, exist_ok=True)
        _copy_show(frozen, scratch)
        show = Show(show_id, root=scratch)

        meter = _CountingLLM(llm, temperature=temperature)
        t0 = time.time()
        error = None
        try:
            res = runner(show, episode, meter, cfg, quality, run_dir)
        except Exception as exc:  # one flaky run must not sink the experiment
            error = f"{type(exc).__name__}: {exc}"
            res = {"artifact": "", "quality": {}, "extra": {}}
        elapsed = time.time() - t0

        artifact = res.get("artifact", "")
        (run_dir / artifact_name).write_text(artifact or "", encoding="utf-8")
        # Keep the exact prompts this run sent, for inspection.
        prompts_src = scratch / "runs" / f"EP{episode:02d}" / "prompts"
        if prompts_src.exists():
            shutil.copytree(prompts_src, run_dir / "prompts", dirs_exist_ok=True)

        if artifact:
            artifacts.append(artifact)
        run_reports.append({
            "index": i,
            "error": error,
            "artifact": str((run_dir / artifact_name).relative_to(out_dir)),
            "elapsed_s": round(elapsed, 1),
            "calls": meter.calls,
            "providers": dict(meter.providers),
            "models": dict(meter.models),
            "structural": structural(artifact),
            "quality": res.get("quality", {}),
            "extra": res.get("extra", {}),
        })
        status = "ERROR " + error if error else "ok"
        progress(f"[eval] run {i}/{runs}: {status} "
                 f"({meter.calls} calls, {elapsed:.1f}s, "
                 f"{len(artifact)} chars, providers={dict(meter.providers)})")

    var = variance(artifacts)
    report = {
        "show": show_id, "episode": episode, "stage": stage, "runs": runs,
        "label": label, "temperature": temperature, "quality": quality,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "frozen_inputs": fingerprint,
        "variance": var,
        "aggregate": _aggregate(run_reports),
        "runs_detail": run_reports,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "metrics.csv", run_reports)
    _print_summary(report, out_dir)
    return report


def _aggregate(run_reports: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in run_reports if not r["error"]]
    if not ok:
        return {"runs_ok": 0, "runs_failed": len(run_reports)}
    pass_rates = [r["quality"].get("pass_rate") for r in ok
                  if isinstance(r["quality"].get("pass_rate"), (int, float))]
    fails = [len(r["quality"].get("failing") or []) for r in ok]
    words = [r["structural"].get("words", 0) for r in ok]
    return {
        "runs_ok": len(ok), "runs_failed": len(run_reports) - len(ok),
        "calls_total": sum(r["calls"] for r in ok),
        "seconds_mean": round(statistics.fmean([r["elapsed_s"] for r in ok]), 1),
        "words_mean": round(statistics.fmean(words), 1) if words else None,
        "pass_rate_mean": round(statistics.fmean(pass_rates), 3) if pass_rates else None,
        "failing_mean": round(statistics.fmean(fails), 1) if fails else None,
    }


def _write_csv(path: Path, run_reports: list[dict[str, Any]]) -> None:
    cols = ["index", "error", "words", "chars", "unique_words", "pass_rate",
            "failing", "calls", "elapsed_s", "providers"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in run_reports:
            q = r["quality"]
            w.writerow([
                r["index"], r["error"] or "", r["structural"].get("words", ""),
                r["structural"].get("chars", ""), r["structural"].get("unique_words", ""),
                q.get("pass_rate", ""), len(q.get("failing") or []),
                r["calls"], r["elapsed_s"],
                "|".join(f"{k}:{v}" for k, v in r["providers"].items()),
            ])


def _print_summary(report: dict[str, Any], out_dir: Path) -> None:
    v = report["variance"]
    a = report["aggregate"]
    print("")
    print(f"  stage={report['stage']}  runs={report['runs']}  "
          f"inputs={report['frozen_inputs']['hash']}")
    if v["similarity_mean"] is not None:
        print(f"  VARIANCE   similarity mean={v['similarity_mean']} "
              f"min={v['similarity_min']} max={v['similarity_max']} "
              f"| jaccard={v['jaccard_mean']} | diversity={v['diversity']}")
        print("             (similarity ~1.0 == runs nearly identical)")
    print(f"  QUALITY    pass_rate={a.get('pass_rate_mean')} "
          f"failing_mean={a.get('failing_mean')}")
    print(f"  COST       calls_total={a.get('calls_total')} "
          f"sec_mean={a.get('seconds_mean')} words_mean={a.get('words_mean')}")
    print(f"  REPORT     {out_dir / 'report.json'}")
    print(f"             {out_dir / 'metrics.csv'}")


# ---------------------------------------------------------------------------
# Compare two reports (baseline vs candidate)
# ---------------------------------------------------------------------------

def _dot(report: dict[str, Any], *path: str) -> Any:
    cur: Any = report
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def compare_reports(a: dict[str, Any], b: dict[str, Any],
                    label_a: str = "A", label_b: str = "B") -> dict[str, Any]:
    rows = [
        ("similarity_mean", ("variance", "similarity_mean"), True),
        ("diversity", ("variance", "diversity"), True),
        ("pass_rate_mean", ("aggregate", "pass_rate_mean"), True),
        ("failing_mean", ("aggregate", "failing_mean"), False),
        ("words_mean", ("aggregate", "words_mean"), None),
        ("calls_total", ("aggregate", "calls_total"), None),
        ("sec_mean", ("aggregate", "seconds_mean"), None),
    ]
    out: list[dict[str, Any]] = []
    for name, path, higher_better in rows:
        va, vb = _dot(a, *path), _dot(b, *path)
        delta = None
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            delta = round(vb - va, 4)
        out.append({"metric": name, "a": va, "b": vb, "delta": delta,
                    "higher_is_better": higher_better})
    return {"a": label_a, "b": label_b, "rows": out}


def print_comparison(cmp: dict[str, Any]) -> None:
    print(f"  {'metric':18s} {cmp['a'][:14]:>14s} {cmp['b'][:14]:>14s} {'delta':>10s}")
    for r in cmp["rows"]:
        a = "" if r["a"] is None else r["a"]
        b = "" if r["b"] is None else r["b"]
        d = "" if r["delta"] is None else (f"+{r['delta']}" if r["delta"] > 0 else str(r["delta"]))
        print(f"  {r['metric']:18s} {str(a):>14s} {str(b):>14s} {d:>10s}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one stage N times and measure variance/quality/cost")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run an experiment")
    run.add_argument("--show", required=True)
    run.add_argument("--episode", type=int, default=1)
    run.add_argument("--stage", choices=STAGES, default="prose")
    run.add_argument("--runs", type=int, default=5)
    run.add_argument("--temperature", type=float, default=None,
                     help="override the sampler temperature (else the stage's own)")
    run.add_argument("--no-quality", action="store_true",
                     help="skip the reviewer pass (variance/cost only)")
    run.add_argument("--label", default="")
    run.add_argument("--out", default=None)
    cmp_p = sub.add_parser("compare", help="compare two report.json files")
    cmp_p.add_argument("a")
    cmp_p.add_argument("b")
    args = ap.parse_args(argv)

    if args.cmd == "compare":
        a = json.loads(Path(args.a).read_text(encoding="utf-8"))
        b = json.loads(Path(args.b).read_text(encoding="utf-8"))
        print_comparison(compare_reports(a, b, args.a, args.b))
        return 0

    run_eval(args.show, args.episode, args.stage, args.runs,
             temperature=args.temperature, quality=not args.no_quality,
             out_dir=args.out, label=args.label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
