"""check_show_ideas validator: names the pitches to rewrite."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from studio.board.roles import load_roles
from studio.config import Config
from tools.check_show_ideas import check


def _pitch(pid, title, slug=""):
    return {"id": pid, "title": title, "show_id": slug or title.lower().replace(" ", "-"),
            "logline": "l", "premise": "p", "genre": ["sf"], "tone": ["dry"],
            "hook": "h", "maturity": "m"}


def test_clean_batch_passes():
    archive = {"pitches": [
        _pitch("p1", "Salt Meridian"),
        _pitch("p2", "Glass Harvest"),
        _pitch("p3", "The Ninth Tide"),
    ]}
    assert check(archive) == []


def test_missing_fields_bad_slug_and_duplicates_flagged():
    archive = {"pitches": [
        _pitch("p1", "The Salt Meridian Chronicles"),
        {"id": "p2", "title": "Glass Harvest"},  # missing fields
        _pitch("p3", "The Salt Meridian Chronicles"),  # duplicate title + slug
        _pitch("p4", "Chrome Tide", "the-salt-meridian-chronicles"),  # dup slug
        _pitch("p5", "The Salt Meridian Chronicles Rising"),  # near-dup title
        _pitch("p6", "Ok Title", "Bad_Slug!"),  # bad slug
    ]}
    fails: dict[str, list[str]] = {}
    for f in check(archive):
        fails.setdefault(f["id"], []).append(f["reason"])
    assert any("missing fields" in r for r in fails.get("p2", []))
    assert any("duplicate" in r for r in fails.get("p3", []))
    assert any("duplicate show_id" in r for r in fails.get("p4", []))
    assert any("similar" in r for r in fails.get("p5", []))
    assert any("bad show_id" in r for r in fails.get("p6", []))
    assert "p1" not in fails  # the original stays clean


def test_draft_helper_writes_archive_from_card(tmp_path, monkeypatch):
    import draft_show_ideas
    from studio.board.store import BoardStore
    from studio.config import Config
    from studio.show import Show

    cfg = Config(tmp_path)
    path = cfg.show_path("alpha")
    (path / "board").mkdir(parents=True, exist_ok=True)
    store = BoardStore(path / "board" / "board.db")
    store.upsert({
        "id": "g0.alpha.ideas", "kind": "gate0_ideas", "title": "t",
        "show_id": "alpha", "role": "gate0_ideas", "reviewer": "human",
        "lane": "llm", "state": "planned", "priority": 10,
        "payload": {"brief": "ships", "n": 2, "auto_id": True},
        "result": {}, "artifacts": [], "notes": [], "units": {},
        "depends_on": [],
    })
    show = Show("alpha", root=path)

    def fake_propose(brief, n=5, cfg=None, llm=None):
        assert brief == "ships" and n == 2
        return [
            {"title": "Salt Meridian", "logline": "l", "premise": "p",
             "genre": ["sf"], "tone": ["dry"], "hook": "h", "maturity": "m"},
            {"title": "Glass Harvest", "logline": "l", "premise": "p",
             "genre": ["sf"], "tone": ["dry"], "hook": "h", "maturity": "m"},
        ]

    monkeypatch.setattr("studio.proposals.propose_concepts", fake_propose)
    out = draft_show_ideas.run("g0.alpha.ideas", cfg=cfg, store=store,
                               show=show)
    assert out["pitches"] == 2
    archive = json.loads((path / "ideas.json").read_text(encoding="utf-8"))
    assert [p["show_id"] for p in archive["pitches"]] == [
        "salt-meridian", "glass-harvest"]
    store.close()


def test_ideas_role_is_small_model_producer():
    cfg = Config(ROOT)
    roles = load_roles(cfg)
    role = roles["gate0_ideas"]
    assert role.kind == "producer"
    assert role.skill == "show-ideas"
    assert role.model == "gemma4-12b-qat-uncensored-hauhaucs-balanced"
    assert role.max_steps <= 15
    assert (ROOT / ".agents" / "skills" / "show-ideas" / "SKILL.md").exists()
