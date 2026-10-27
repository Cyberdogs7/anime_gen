"""Per-show board storage: one DB per show, legacy migration, delete cleanup."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board.cards import new_card
from studio.board.daemon import request_episode
from studio.board.store import BoardSet, BoardStore
from studio.config import Config


def _cfg(tmp_path) -> Config:
    cfg = Config(tmp_path)
    for sid in ("alpha", "beta"):
        path = cfg.show_path(sid)
        path.mkdir(parents=True, exist_ok=True)
        (path / "bible.yaml").write_text("title: x\n", encoding="utf-8")
    return cfg


def _card(card_id: str, show_id: str, title: str = "t") -> dict:
    return new_card(card_id, kind="outline", title=title, role="showrunner",
                    lane="llm", show_id=show_id, episode=1)


def test_board_set_writes_one_db_per_show(tmp_path):
    cfg = _cfg(tmp_path)
    store = BoardSet(cfg)
    store.create(_card("ep01.outline", "alpha", "a"))
    store.create(_card("ep01.outline", "beta", "b"))

    assert (cfg.show_path("alpha") / "board" / "board.db").exists()
    assert (cfg.show_path("beta") / "board" / "board.db").exists()
    assert store.get("ep01.outline", show_id="alpha")["title"] == "a"
    assert store.get("ep01.outline", show_id="beta")["title"] == "b"
    assert len(store.all()) == 2
    store.close()


def test_board_set_migrates_legacy_central_cards(tmp_path):
    cfg = _cfg(tmp_path)
    legacy = BoardStore(cfg.db_path)
    legacy.create(_card("g0.alpha.plan", "alpha"))
    legacy.note("g0.alpha.plan", "human", "kept")
    legacy.close()

    store = BoardSet(cfg)

    card = store.get("g0.alpha.plan", show_id="alpha")
    assert card is not None and card["notes"][0]["text"] == "kept"
    assert (cfg.show_path("alpha") / "board" / "board.db").exists()
    assert BoardStore(cfg.db_path).all() == []
    store.close()


def test_daemon_tick_works_on_board_set(tmp_path):
    from studio.board.daemon import BoardDaemon

    cfg = _cfg(tmp_path)
    store = BoardSet(cfg)
    store.create(new_card("alpha.ep01.bad", kind="outline", title="t",
                          role="no-such-role", lane="llm",
                          show_id="alpha", episode=1))
    daemon = BoardDaemon(store, cfg)

    actions = daemon.tick()

    assert any("blocked" in action for action in actions)
    assert store.get("alpha.ep01.bad", show_id="alpha")["state"] == "blocked"
    store.close()


def test_request_episode_uses_the_show_board(tmp_path):
    cfg = _cfg(tmp_path)
    store = BoardSet(cfg)
    request_episode(store, "alpha", 1)
    request_episode(store, "beta", 1)

    assert store.get("alpha.ep01.outline", show_id="alpha")
    assert store.get("beta.ep01.outline", show_id="beta")
    assert (cfg.show_path("alpha") / "board" / "board.db").exists()
    assert (cfg.show_path("beta") / "board" / "board.db").exists()
    store.close()


def test_delete_show_carries_the_board_db_to_trash(tmp_path, monkeypatch):
    import studio.config as cfg_mod
    from studio.config import get_config
    from studio.shows import delete_show

    monkeypatch.setattr(cfg_mod, "_default", cfg_mod.Config(tmp_path))
    cfg = get_config()
    path = cfg.show_path("alpha")
    path.mkdir(parents=True, exist_ok=True)
    (path / "bible.yaml").write_text("title: x\n", encoding="utf-8")

    store = BoardSet(cfg)
    store.create(_card("alpha.ep01.outline", "alpha"))
    assert (path / "board" / "board.db").exists()

    delete_show("alpha")

    assert not path.exists()
    assert (cfg.root / ".trash" / "alpha" / "board" / "board.db").exists()
    store.close()
