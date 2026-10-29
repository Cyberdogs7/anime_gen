"""Adopting a show folder: rebuild cards from on-disk state without an LLM."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board.adopt import MARKER_KEY
from studio.board.cards import new_card
from studio.board.store import BoardSet, BoardStore
from studio.config import Config


def _show(cfg: Config, show_id: str = "alpha") -> Path:
    path = cfg.show_path(show_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "bible.yaml").write_text("title: Alpha\n", encoding="utf-8")
    return path


def _episode(path: Path, *, shots=("s01_sh01",), rendered=None, keyframed=None) -> None:
    ep = path / "runs" / "EP01"
    ep.mkdir(parents=True, exist_ok=True)
    (ep / "plan.json").write_text(json.dumps(
        {"status": "approved", "objects": [], "scenes": [{"id": "s01"}]}),
        encoding="utf-8")
    (ep / "scene_details.json").write_text(json.dumps(
        {"status": "approved", "scenes": [{"id": "s01"}]}), encoding="utf-8")
    (ep / "script.r1.json").write_text(json.dumps({
        "_review": {"passed": True},
        "scenes": [{"shots": [{"id": sid} for sid in shots]}],
    }), encoding="utf-8")
    for sid in (keyframed if keyframed is not None else shots):
        (ep / "storyboard").mkdir(parents=True, exist_ok=True)
        (ep / "storyboard" / f"{sid}.mp4").write_bytes(b"x")
    for sid in (rendered if rendered is not None else shots):
        (ep / "video").mkdir(parents=True, exist_ok=True)
        (ep / "video" / f"{sid}.mp4").write_bytes(b"x")
    (ep / "qc").mkdir(parents=True, exist_ok=True)
    (ep / "qc" / "summary.json").write_text("{}", encoding="utf-8")
    (ep / "episode.mp4").write_bytes(b"x")


def _bootstrap_complete(path: Path) -> None:
    (path / "bootstrap.json").write_text(json.dumps({
        "concept": {"status": "approved"},
        "bible": {"status": "approved"},
        "scenes": {"status": "approved"},
        "characters": [{"name": "A", "proposal": "approved",
                        "refs": "approved", "voice": "approved"}],
        "complete": True,
    }), encoding="utf-8")


def test_adopt_complete_folder_marks_everything_accepted(tmp_path):
    cfg = Config(tmp_path)
    path = _show(cfg)
    _bootstrap_complete(path)
    _episode(path)

    store = BoardSet(cfg, adopt=True)

    db = BoardStore(path / "board" / "board.db")
    assert db.meta_get(MARKER_KEY)
    db.close()
    for domain in ("ideas", "bible", "characters", "scenes",
                   "images", "voices"):
        assert store.get(f"g0.alpha.{domain}")["state"] == "accepted", domain
    for kind in ("outline", "locations", "cast", "outfits", "objects",
                 "narrative", "dialogue", "shots", "camera", "action",
                 "sound", "h3prompts", "keyframes", "render",
                 "qc", "assemble"):
        assert store.get(f"alpha.ep01.{kind}")["state"] == "accepted", kind
    store.close()


def test_adopt_partial_render_is_blocked_not_replanned(tmp_path):
    cfg = Config(tmp_path)
    path = _show(cfg)
    _bootstrap_complete(path)
    _episode(path, shots=("s01_sh01", "s01_sh02"), rendered=("s01_sh01",))

    store = BoardSet(cfg, adopt=True)

    card = store.get("alpha.ep01.render")
    assert card["state"] == "blocked"
    assert any("partial render" in n["text"] for n in card["notes"])
    store.close()


def test_adopt_missing_bootstrap_queues_gate0_only(tmp_path):
    cfg = Config(tmp_path)
    _show(cfg)

    store = BoardSet(cfg, adopt=True)
    cards = store.all(show_id="alpha")

    assert {c["id"] for c in cards} == {
        "g0.alpha.ideas", "g0.alpha.bible", "g0.alpha.characters",
        "g0.alpha.scenes", "g0.alpha.images", "g0.alpha.voices"}
    assert all(c["state"] == "planned" for c in cards)
    store.close()


def test_adopt_is_one_time(tmp_path):
    cfg = Config(tmp_path)
    path = _show(cfg)
    _bootstrap_complete(path)
    _episode(path)

    first = BoardSet(cfg, adopt=True)
    for card in first.all(show_id="alpha"):
        first.delete_card(card["id"])
    first.close()

    second = BoardSet(cfg, adopt=True)
    assert second.all(show_id="alpha") == []
    second.close()


def test_adopt_leaves_a_restored_board_db_alone(tmp_path):
    cfg = Config(tmp_path)
    path = _show(cfg)
    db = path / "board" / "board.db"
    db.parent.mkdir(parents=True)
    board = BoardStore(db)
    board.create(new_card("alpha.ep01.outline", kind="outline", title="t",
                          role="showrunner", lane="llm",
                          show_id="alpha", episode=1))
    board.close()

    store = BoardSet(cfg, adopt=True)
    cards = store.all(show_id="alpha")

    assert len(cards) == 1 and cards[0]["id"] == "alpha.ep01.outline"
    store.close()
